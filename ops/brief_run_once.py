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


def _re_render_docs(runner: CounterpartyBriefRunner, spec: str) -> int:
    """Iterate matching CounterpartyBrief rows, rebuild the Doc
    body from cached payload using current Markdown→HTML +
    doc_body code, upload as a NEW Google Doc, and update the
    row's `google_doc_url`/`google_doc_id`. Old Docs are NOT
    deleted (they keep working but the brief now points at the
    fresh version)."""
    from app.counterparty_briefs.doc import (
        build_doc_title,
        build_org_doc_body,
        build_person_doc_body,
    )
    from app.counterparty_briefs.extract import BeneficiaryCandidate
    from app.counterparty_briefs.research import _coerce_org, _coerce_person

    keys: list[str] = []
    if spec == "all":
        keys = ["all"]
    else:
        keys = [
            normalise_counterparty_name(k)
            for k in spec.split(",") if k.strip()
        ]
    with session_scope() as s:
        q = s.query(CounterpartyBrief)
        if keys and keys != ["all"]:
            q = q.filter(CounterpartyBrief.counterparty_key.in_(keys))
        rows = q.all()
        if not rows:
            log.warning("brief_run_once_re_render_no_rows", spec=spec)
            return 1
        log.info("brief_run_once_re_render_starting", row_count=len(rows))
        for row in rows:
            payload = row.research_payload or {}
            if row.kind == "org":
                research = _coerce_org(
                    {**payload, "name": payload.get("name") or row.display_name}
                )
                body = build_org_doc_body(
                    org_name=row.display_name, context=None, research=research,
                )
                title = build_doc_title(
                    kind="org", display_name=row.display_name,
                    scheduled_at=datetime.now(timezone.utc),
                )
            else:
                research = _coerce_person(payload)
                body = build_person_doc_body(
                    beneficiary=BeneficiaryCandidate(
                        person_name=row.display_name, person_role=None,
                    ),
                    context=None,
                    research=research,
                )
                title = build_doc_title(
                    kind="person", display_name=row.display_name,
                    scheduled_at=datetime.now(timezone.utc),
                )
            doc_url, doc_id = runner._maybe_create_doc(title=title, body=body)
            if doc_url:
                row.google_doc_url = doc_url
                row.google_doc_id = doc_id
                log.info(
                    "brief_run_once_re_rendered",
                    key=row.counterparty_key, kind=row.kind, new_url=doc_url,
                )
            else:
                log.warning(
                    "brief_run_once_re_render_failed",
                    key=row.counterparty_key, kind=row.kind,
                )
    return 0


def _seed_existing_events(
    runner: CounterpartyBriefRunner, lookahead_days: int,
) -> int:
    """Mark every event in the current Calendar window as
    already-processed so the daemon will skip them on the next
    tick. Filters by the same organizer/creator gate the daemon
    applies, so only meetings the daemon WOULD have briefed get
    seeded — internal events stay un-touched.

    No LLM calls. No Slack posts. No Docs created.
    """
    from decimal import Decimal

    from app.counterparty_briefs.runner import (
        event_already_processed,
        event_passes_host_gate,
    )
    from app.models import CounterpartyBriefsEvent

    events = _fetch_events_wide(
        runner, lookahead_days=lookahead_days, lookback_days=0,
    )
    if not events:
        log.info("brief_run_once_seed_no_events")
        return 0

    seeded = 0
    skipped_host = 0
    skipped_done = 0
    with session_scope() as s:
        for ev in events:
            ev_id = (ev or {}).get("id") or ""
            if not ev_id:
                continue
            if not event_passes_host_gate(ev, runner._operator_email):
                skipped_host += 1
                continue
            if event_already_processed(s, calendar_event_id=ev_id):
                skipped_done += 1
                continue
            start = ev.get("start")
            scheduled_at = (
                start if isinstance(start, datetime)
                else datetime.now(timezone.utc)
            )
            s.add(CounterpartyBriefsEvent(
                calendar_event_id=ev_id,
                event_title=ev.get("title") or "",
                scheduled_meeting_at=scheduled_at,
                posted_at=datetime.now(timezone.utc),
                slack_channel=(
                    runner._settings.counterparty_briefs_slack_target_channel_id
                ),
                slack_ts=None,
                total_cost_usd=Decimal("0.0"),
                link_summary=[{"note": "seeded_as_existing"}],
            ))
            seeded += 1
    log.info(
        "brief_run_once_seed_done",
        seeded=seeded,
        skipped_host=skipped_host,
        already_processed=skipped_done,
        total_events=len(events),
    )
    return 0


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
    parser.add_argument(
        "--re-render-docs", default="",
        help="comma-separated counterparty keys (or 'all'); "
             "regenerates the Google Doc for each matching brief "
             "row from the cached research payload — no LLM calls. "
             "Use after Doc-body / Markdown-to-HTML changes to "
             "refresh in-DB Doc URLs.",
    )
    parser.add_argument(
        "--seed-existing-events", action="store_true",
        help="pre-populate counterparty_briefs_events idempotency "
             "rows for every Calendar event in the current window "
             "WITHOUT calling the LLM or posting to Slack. Use "
             "BEFORE turning the daemon on so it skips meetings "
             "that already exist and only sends briefs for events "
             "created later.",
    )
    args = parser.parse_args()

    runner = _build_runner_for_oneshot()
    if runner is None:
        return 2

    # --re-render-docs: regenerate Doc for matching briefs from
    # cached payload (no LLM). Useful after markdown_to_html /
    # doc-body changes.
    if args.re_render_docs:
        return _re_render_docs(runner, args.re_render_docs)

    # --seed-existing-events: mark already-existing meetings as
    # processed so the daemon only acts on NEW ones.
    if args.seed_existing_events:
        return _seed_existing_events(runner, args.lookahead_days)

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
        from app.counterparty_briefs.extract import (
            extract_event_counterparties,
        )
        from app.counterparty_briefs.runner import event_passes_host_gate

        events_sorted = sorted(
            events,
            key=lambda e: (
                e.get("start").isoformat()
                if hasattr(e.get("start"), "isoformat") else str(e.get("start"))
            ),
        )
        # Cheap LLM (extract model) — run Stage 0 per event so the
        # operator sees who would actually be researched.
        extract_model = (
            runner._settings.counterparty_briefs_extract_model
            or runner._settings.openai_model
        )
        # Pre-load idempotency rows so we can flag already-processed.
        with session_scope() as s:
            processed_ids = {
                row.calendar_event_id
                for row in s.query(CounterpartyBriefsEvent).all()
            }

        print(f"\n# {len(events_sorted)} events in window\n")
        for e in events_sorted:
            start = e.get("start")
            start_s = (
                start.strftime("%Y-%m-%d %H:%M")
                if hasattr(start, "strftime") else str(start)
            )
            org_email = (e.get("organizer") or {}).get("email") or "?"
            flags: list[str] = []
            if not event_passes_host_gate(e, runner._operator_email):
                flags.append("SKIP:host")
            if e.get("id") in processed_ids:
                flags.append("DONE")
            flag_s = f"  [{', '.join(flags)}]" if flags else ""
            print(
                f"\n{start_s}  {e.get('id')}{flag_s}\n"
                f"  organizer: {org_email}\n"
                f"  title:     {e.get('title')}"
            )
            if "SKIP:host" in flags:
                continue
            try:
                ex = extract_event_counterparties(
                    event=e, llm_backend=runner._llm, model=extract_model,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  extract:   ERROR ({exc})")
                continue
            org_name = ex.org_name or "—"
            print(f"  org:       {org_name}")
            if ex.initial_persons:
                for p in ex.initial_persons:
                    role = f" ({p.person_role})" if p.person_role else ""
                    print(f"    👤 {p.person_name}{role}")
            else:
                print("    (no initial persons — beneficiaries picked at stage 2)")
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
