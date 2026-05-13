"""FR-CR-05-165 — One-shot CLI to fire the agenda pipeline.

Same code path the daemon runner uses on every tick, but with a
configurable look-ahead window (default 2h vs the runner's
±1min). Useful for:

  * Smoke-testing the full pipeline in production WITHOUT waiting
    for a real `now+10min` event.
  * Backfilling missed events after a deploy hiccup (the
    `meeting_agendas.calendar_event_id` UNIQUE constraint keeps
    re-runs idempotent — re-running a posted event is a no-op
    unless you pass `--force`).

Usage:

    docker compose run --rm bot python -m ops.agenda_run_once \\
        --lookahead-minutes 120

Optional flags:
    --force                — ignore meeting_agendas dedup
                             (delete existing row before posting)
    --calendar-event-id X  — process only this one Calendar event
                             (still requires it to be inside the
                             lookahead window)
    --dry-run              — log what WOULD happen, do not call
                             OpenAI / Docs / Slack, do not write
                             meeting_agendas
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from app.agenda.runner import AgendaRunner
from app.agenda.service import AgendaService, build_candidates
from app.config import get_settings
from app.db import session_scope
from app.intent.llm_backends import OpenAIBackend
from app.logging_setup import get_logger, setup_logging
from app.models import MeetingAgenda
from app.services.calendar_match import (
    fetch_calendar_events_around,
    fetch_calendar_events_via_api,
)
from app.sync.factories import (
    build_calendar_credentials_factory,
    build_docs_factory,
)

log = get_logger(__name__)


def _build_runner_for_oneshot():
    settings = get_settings()

    if not settings.agenda_slack_target_channel_id:
        log.error(
            "agenda_run_once_no_slack_target",
            hint="set AGENDA_SLACK_TARGET_CHANNEL_ID in env",
        )
        return None

    if not settings.openai_api_key:
        log.error("agenda_run_once_no_openai_key")
        return None

    try:
        from openai import OpenAI
        from slack_sdk import WebClient
    except ImportError as e:
        log.error("agenda_run_once_sdk_missing", error=str(e))
        return None

    slack_client = WebClient(token=settings.slack_bot_token)
    llm_backend = OpenAIBackend(
        OpenAI(api_key=settings.openai_api_key),
        settings.openai_model,
    )

    return AgendaRunner(
        settings=settings,
        slack_client=slack_client,
        llm_backend=llm_backend,
        calendar_factory=build_calendar_credentials_factory(settings),
        docs_factory=build_docs_factory(settings),
    )


def _fetch_events_wide(runner: AgendaRunner, lookahead_minutes: int) -> list[dict]:
    """Fetch events in [now, now + lookahead_minutes]. Unlike the
    runner's regular `_fetch_events` this uses a WIDE window (the
    runner is ±1min around `now+lead`)."""
    settings = runner._settings
    now = datetime.now(timezone.utc)
    half = max(1, int(lookahead_minutes)) // 2
    center = now + timedelta(minutes=half)

    events_raw: list[dict] = []
    if runner._calendar_factory is not None:
        try:
            events_raw = fetch_calendar_events_via_api(
                meeting_dt=center,
                window_minutes=half,
                credentials_factory=runner._calendar_factory,
                calendar_id=settings.google_calendar_id or "primary",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("agenda_run_once_api_failed", error=str(e))

    if not events_raw and settings.calendar_apps_script_url:
        try:
            events_raw = fetch_calendar_events_around(
                meeting_dt=center,
                window_minutes=half,
                apps_script_url=settings.calendar_apps_script_url,
                shared_token=settings.calendar_apps_script_shared_token,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("agenda_run_once_apps_script_failed", error=str(e))

    return [runner._normalise_event(ev) for ev in events_raw if ev]


def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(
        description="One-shot agenda pipeline (FR-CR-05-165)",
    )
    parser.add_argument(
        "--lookahead-minutes", type=int, default=120,
        help="search events scheduled within now + N minutes (default 120)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="delete existing meeting_agendas row for the candidate "
             "before posting (re-send forced)",
    )
    parser.add_argument(
        "--calendar-event-id", default="",
        help="process ONLY the event with this id (must still be "
             "inside the lookahead window)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="log what would happen, skip LLM / Docs / Slack / DB writes",
    )
    args = parser.parse_args()

    runner = _build_runner_for_oneshot()
    if runner is None:
        return 2
    settings = runner._settings

    # Step 1 — fetch upcoming events (wide window).
    events = [ev for ev in _fetch_events_wide(runner, args.lookahead_minutes) if ev]
    log.info(
        "agenda_run_once_events_fetched",
        count=len(events),
        lookahead_minutes=args.lookahead_minutes,
    )
    if not events:
        log.info(
            "agenda_run_once_no_events",
            hint=(
                "Calendar returned nothing in the lookahead window. "
                "Try a larger --lookahead-minutes or verify the "
                "calendar id / OAuth credentials."
            ),
        )
        return 0

    if args.calendar_event_id:
        events = [ev for ev in events if ev.get("id") == args.calendar_event_id]
        log.info(
            "agenda_run_once_filtered_to_event",
            calendar_event_id=args.calendar_event_id,
            kept=len(events),
        )
        if not events:
            log.warning(
                "agenda_run_once_event_id_not_in_window",
                calendar_event_id=args.calendar_event_id,
            )
            return 1

    # Step 2 — drop dedup row up front when --force.
    if args.force:
        with session_scope() as session:
            for ev in events:
                deleted = (
                    session.query(MeetingAgenda)
                    .filter(MeetingAgenda.calendar_event_id == ev["id"])
                    .delete()
                )
                if deleted:
                    log.info(
                        "agenda_run_once_force_deleted_dedup_row",
                        calendar_event_id=ev["id"], deleted=deleted,
                    )

    # Step 3 — build candidates (drops already-posted + not-recurring).
    with session_scope() as session:
        candidates = build_candidates(
            session,
            events=events,
            lookback_days=settings.agenda_lookback_days,
            min_prior_meetings=settings.agenda_min_prior_meetings,
        )

    log.info(
        "agenda_run_once_candidates",
        count=len(candidates),
        titles=[c.title for c in candidates],
    )
    if not candidates:
        log.info(
            "agenda_run_once_no_candidates",
            hint=(
                "No events pass build_candidates filter. Reasons: "
                "(a) no prior zoom_recordings with the same normalised "
                "title; (b) already-posted (run with --force); (c) "
                "min_prior_meetings threshold not met."
            ),
        )
        return 0

    # Step 4 — process each.
    posted = 0
    for c in candidates:
        if args.dry_run:
            log.info(
                "agenda_run_once_dry_run_skip",
                calendar_event_id=c.calendar_event_id,
                title=c.title,
                prior_count=len(c.prior_recordings),
                open_tasks_count=len(c.open_tasks),
            )
            continue
        try:
            runner._process_candidate(c)
            posted += 1
        except Exception as e:  # noqa: BLE001
            log.warning(
                "agenda_run_once_process_failed",
                calendar_event_id=c.calendar_event_id, error=str(e),
            )

    log.info("agenda_run_once_done", posted=posted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
