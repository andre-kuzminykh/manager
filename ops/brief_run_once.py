"""FR-CR-05-168 — one-shot Counterparty Briefs CLI.

Usage:

    docker compose run --rm bot python -m ops.brief_run_once \\
        --lookahead-days 7 --dry-run

Flags:
    --lookahead-days N            search events within now+N days
    --lookback-days N             also scan past N days (default 0).
                                  Use this to re-run a brief for a
                                  meeting that already happened.
    --calendar-event-id X         process ONLY this event id
    --force-event ID              delete the events row first
    --force-counterparty NAME     delete the cached brief for this
                                  counterparty (forces a fresh
                                  research call next time)
    --dry-run                     log what would happen, skip all
                                  side effects (LLM/Docs/Slack/DB)
    --limit N                     post at most N events
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.counterparty_briefs.lookup import normalise_counterparty_name
from app.counterparty_briefs.runner import (
    CounterpartyBriefRunner,
    event_already_processed,
)
from app.db import session_scope
from app.intent.llm_backends import OpenAIBackend
from app.logging_setup import get_logger, setup_logging
from app.models import CounterpartyBrief, CounterpartyBriefsEvent
from app.services.calendar_match import fetch_calendar_events_via_api
from app.sync.factories import (
    build_calendar_credentials_factory_with_sa_fallback,
    build_docs_factory,
)

log = get_logger(__name__)


def _build_runner_for_oneshot() -> CounterpartyBriefRunner | None:
    settings = get_settings()

    if not settings.counterparty_briefs_slack_target_channel_id:
        log.error(
            "brief_run_once_no_slack_target",
            hint="set COUNTERPARTY_BRIEFS_SLACK_TARGET_CHANNEL_ID",
        )
        return None
    if not settings.openai_api_key:
        log.error("brief_run_once_no_openai_key")
        return None

    try:
        from openai import OpenAI
        from slack_sdk import WebClient
    except ImportError as e:
        log.error("brief_run_once_sdk_missing", error=str(e))
        return None

    slack_client = WebClient(
        token=(
            settings.agenda_slack_bot_token
            or settings.slack_bot_token
        )
    )
    llm = OpenAIBackend(
        OpenAI(api_key=settings.openai_api_key),
        settings.openai_model,
    )
    return CounterpartyBriefRunner(
        settings=settings,
        slack_client=slack_client,
        llm_backend=llm,
        calendar_factory=build_calendar_credentials_factory_with_sa_fallback(settings),
        docs_factory=build_docs_factory(settings),
    )


def _fetch_events_wide(
    runner: CounterpartyBriefRunner,
    lookahead_days: int,
    lookback_days: int = 0,
) -> list[dict]:
    settings = runner._settings
    now = datetime.now(timezone.utc)
    earliest = now - timedelta(days=max(0, lookback_days))
    latest = now + timedelta(days=max(1, lookahead_days))
    centre = earliest + (latest - earliest) / 2
    half_minutes = int((latest - earliest).total_seconds() / 60 / 2) or 1

    if runner._calendar_factory is None:
        log.warning(
            "brief_run_once_no_calendar_factory",
            hint="Calendar OAuth / SA not configured",
        )
        return []
    try:
        raw = fetch_calendar_events_via_api(
            meeting_dt=centre,
            window_minutes=half_minutes,
            credentials_factory=runner._calendar_factory,
            calendar_id=settings.google_calendar_id or "primary",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("brief_run_once_calendar_failed", error=str(e))
        return []
    return [
        e for e in (runner._normalise_event(ev) for ev in raw)
        if e is not None
    ]


def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(
        description="One-shot Counterparty Briefs (FR-CR-05-168)",
    )
    parser.add_argument("--lookahead-days", type=int, default=7)
    parser.add_argument(
        "--lookback-days", type=int, default=0,
        help="also scan past N days (default 0). Use to re-run a "
             "brief for a meeting that already happened.",
    )
    parser.add_argument("--calendar-event-id", default="")
    parser.add_argument("--force-event", default="")
    parser.add_argument("--force-counterparty", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--skip-slack", action="store_true",
        help="do the full LLM + Docs work but DO NOT post the "
             "grouped Slack DM (operator-test mode — lets you "
             "inspect generated Docs before the daemon goes live)",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="post at most N events (0 = unlimited)",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="just print events in the window (id / start / title) "
             "and exit — useful for picking a --calendar-event-id "
             "to re-run.",
    )
    args = parser.parse_args()

    runner = _build_runner_for_oneshot()
    if runner is None:
        return 2

    # --force-event: delete the events idempotency row so the
    # event is reprocessed.
    if args.force_event:
        with session_scope() as s:
            deleted = (
                s.query(CounterpartyBriefsEvent)
                .filter(
                    CounterpartyBriefsEvent.calendar_event_id == args.force_event
                )
                .delete()
            )
            log.info(
                "brief_run_once_force_event_deleted",
                calendar_event_id=args.force_event, deleted=deleted,
            )

    # --force-counterparty: delete the cached brief so the next
    # event referencing it triggers a fresh research call.
    if args.force_counterparty:
        key = normalise_counterparty_name(args.force_counterparty)
        with session_scope() as s:
            deleted = (
                s.query(CounterpartyBrief)
                .filter(CounterpartyBrief.counterparty_key == key)
                .delete()
            )
            log.info(
                "brief_run_once_force_counterparty_deleted",
                counterparty_key=key, deleted=deleted,
            )

    events = _fetch_events_wide(
        runner, args.lookahead_days, lookback_days=args.lookback_days,
    )
    log.info(
        "brief_run_once_events_fetched",
        count=len(events),
        lookahead_days=args.lookahead_days,
        lookback_days=args.lookback_days,
    )

    if args.list:
        events_sorted = sorted(
            events,
            key=lambda e: (
                e.get("start").isoformat()
                if hasattr(e.get("start"), "isoformat") else str(e.get("start"))
            ),
        )
        print(f"\n# {len(events_sorted)} events in window\n")
        for e in events_sorted:
            start = e.get("start")
            start_s = (
                start.strftime("%Y-%m-%d %H:%M")
                if hasattr(start, "strftime") else str(start)
            )
            org = (e.get("organizer") or {}).get("email") or "?"
            print(
                f"{start_s}  {e.get('id'):<60s}  "
                f"[{org}]  {e.get('title')}"
            )
        return 0
    if args.calendar_event_id:
        events = [e for e in events if e.get("id") == args.calendar_event_id]
        log.info(
            "brief_run_once_filtered_to_event",
            calendar_event_id=args.calendar_event_id, kept=len(events),
        )
        if not events:
            log.warning(
                "brief_run_once_event_id_not_in_window",
                calendar_event_id=args.calendar_event_id,
            )
            return 1

    if not events:
        log.info("brief_run_once_no_events", hint="Calendar empty for window")
        return 0

    # Drop events that are already in the idempotency table —
    # unless --force-event was specified for them.
    with session_scope() as s:
        events = [
            e for e in events
            if not event_already_processed(
                s, calendar_event_id=e.get("id") or ""
            )
        ]
    if not events:
        log.info("brief_run_once_all_processed")
        return 0

    if args.limit and args.limit > 0:
        events = events[: args.limit]

    posted = 0
    for ev in events:
        if args.dry_run:
            log.info(
                "brief_run_once_dry_run",
                event_id=ev.get("id"),
                title=ev.get("title"),
                start=str(ev.get("start")),
            )
            continue
        try:
            runner.process_event(ev, skip_slack=args.skip_slack)
            posted += 1
        except Exception as e:  # noqa: BLE001
            log.warning(
                "brief_run_once_process_failed",
                event_id=ev.get("id"), error=str(e),
            )
    log.info("brief_run_once_done", posted=posted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
