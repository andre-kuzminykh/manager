#!/usr/bin/env python3
"""Manual brief trigger — pick a specific upcoming Calendar event,
run the full briefs pipeline NOW (Stage 0 + org research + Stage 2
+ per-person research + Google Doc + Slack DM), let the operator
see the real post in Slack.

Operator-pinned: «как мне точно быть во всем убежден что реально
работает, что реально отправляется?» — this script doesn't simulate,
it produces a live Slack DM identical to what the morning runner
would have sent (same code path, just operator-controlled timing).

Usage:
    docker compose exec -T bot python -m ops.post_brief_now \\
        --title-contains "Baris Yildiz"
    docker compose exec -T bot python -m ops.post_brief_now \\
        --event-id "abc123"
    docker compose exec -T bot python -m ops.post_brief_now \\
        --title-contains "Apple" --dry-run

`--dry-run` skips the Slack post (still builds the Doc + computes
everything else), useful for «look at the artifact before paying
for the Slack noise».
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from slack_sdk import WebClient

from app.config import get_settings
from app.counterparty_briefs.runner import CounterpartyBriefRunner
from app.intent.llm_backends import OpenAIBackend
from app.services.calendar_match import fetch_calendar_events_via_api
from app.sync.factories import (
    build_calendar_credentials_factory_with_sa_fallback,
    build_docs_factory,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--title-contains", default=None,
        help="Substring of the Calendar event title to match.",
    )
    ap.add_argument("--event-id", default=None)
    ap.add_argument(
        "--lookahead-days", type=int, default=14,
        help="How many days ahead to search Calendar (default 14).",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Build everything but don't post to Slack.",
    )
    args = ap.parse_args()

    if not args.title_contains and not args.event_id:
        print("ERROR: pass --title-contains OR --event-id",
              file=sys.stderr)
        return 2

    s = get_settings()
    if not s.openai_api_key:
        print("ERROR: OPENAI_API_KEY not set.", file=sys.stderr)
        return 2

    cal_factory = build_calendar_credentials_factory_with_sa_fallback(s)
    if cal_factory is None:
        print("ERROR: Calendar factory unavailable (OAuth + SA both "
              "missing).", file=sys.stderr)
        return 2

    # 1. Find the target event.
    print("[1/4] Fetching Calendar events for the next "
          f"{args.lookahead_days} days…")
    now = datetime.now(timezone.utc)
    # Centre the window; large window gives us 1d before + ahead.
    target_dt = now + timedelta(days=max(0, args.lookahead_days // 2))
    window_minutes = max(60, (args.lookahead_days * 24 * 60) // 2)
    events = fetch_calendar_events_via_api(
        meeting_dt=target_dt,
        window_minutes=window_minutes,
        credentials_factory=cal_factory,
        calendar_id=s.google_calendar_id,
    ) or []
    print(f"  → {len(events)} events in window")

    target = None
    if args.event_id:
        target = next(
            (e for e in events if (e.get("id") or "") == args.event_id),
            None,
        )
    else:
        needle = args.title_contains.lower()
        candidates = [
            e for e in events
            if needle in (e.get("title") or e.get("summary") or "").lower()
        ]
        if not candidates:
            print(f"  ❌ no event title contains {args.title_contains!r}")
            return 1
        if len(candidates) > 1:
            print(f"  ⚠️  {len(candidates)} candidates:")
            for c in candidates:
                print(f"    - [{c.get('id')}] {c.get('title')} "
                      f"@ {c.get('start')}")
            target = candidates[0]
            print(f"  picking first: {target.get('id')}")
        else:
            target = candidates[0]

    if target is None:
        print("  ❌ no target event picked")
        return 1
    print(f"  → target: [{target.get('id')}] {target.get('title')!r} "
          f"@ {target.get('start')}")
    print(f"    attendees: {len(target.get('attendees') or [])}")

    # 2. Wire the runner. We don't start its loop — just call
    # process_event directly.
    print("\n[2/4] Wiring runner…")
    openai_client = None
    try:
        from openai import OpenAI as _OpenAI
        openai_client = _OpenAI(api_key=s.openai_api_key)
    except Exception as e:  # noqa: BLE001
        print(f"  ERROR: OpenAI client init failed: {e}", file=sys.stderr)
        return 3
    extract_model = (s.counterparty_briefs_extract_model or "").strip() \
        or "gpt-4o-mini"
    llm = OpenAIBackend(client=openai_client, model=extract_model)

    bot_token = (
        s.agenda_slack_bot_token
        or s.ceo_brain_slack_bot_token
        or s.slack_bot_token
    )
    if not bot_token:
        print("ERROR: no Slack bot token configured.", file=sys.stderr)
        return 4
    slack = WebClient(token=bot_token)

    runner = CounterpartyBriefRunner(
        settings=s,
        slack_client=slack,
        llm_backend=llm,
        calendar_factory=cal_factory,
        docs_factory=build_docs_factory(s),
    )
    print("  → ready")

    # 3. Process the event end-to-end. `skip_slack=args.dry_run`
    # builds the Doc + per-person research + persists the briefs
    # cache, but does NOT actually post to Slack.
    print(f"\n[3/4] Running process_event "
          f"(dry-run={args.dry_run})…")
    print("  → THIS WILL POST A REAL SLACK DM UNLESS --dry-run IS SET")
    runner.process_event(target, skip_slack=args.dry_run)

    print("\n[4/4] Done.")
    if args.dry_run:
        print("  --dry-run was set; Slack DM was NOT sent. Doc(s) were "
              "created and briefs cached so the next non-dry-run is "
              "instant.")
    else:
        print("  Check the operator's Slack DM with Humanoid CEO Brain / "
              "agenda bot — the brief should be there.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
