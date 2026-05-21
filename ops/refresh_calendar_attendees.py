"""FR-CR-05-192 — Refresh ``calendar_attendees`` from live Google
Calendar (in case the original ingest matched the wrong event or
missed attendees), then regenerate ``short_summary`` so the
«Участники:» line reflects the full canonical list.

Walks each ZoomRecording + MeetingRecording in the date range,
calls the same ``_populate_calendar_attendees`` the pipeline uses,
prints before/after attendees-count diff, then re-runs
``_step_short_summary`` only.

Usage:
    docker exec manager-bot-1 python -m ops.refresh_calendar_attendees \\
        --start 2026-05-19 --end 2026-05-22 \\
        --only-artem [--dry-run]
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone

from app.config import get_settings
from app.db import session_scope
from app.intent.llm_backends import OpenAIBackend
from app.models import MeetingRecording, ZoomRecording
from app.sync.factories import (
    build_calendar_credentials_factory_with_sa_fallback,
)


def _has_artem(s: str) -> bool:
    for line in (s or "").split("\n"):
        if line.startswith("Участники:"):
            ll = line.lower()
            return any(n in ll for n in [
                "артем", "артём", "artem", "sokolov", "соколов",
            ])
    return False


def _attendees_summary(attendees: list) -> str:
    if not attendees:
        return "0 attendees"
    names = []
    for a in attendees[:8]:
        if not isinstance(a, dict):
            continue
        nm = a.get("resolved_name") or a.get("display_name") or a.get("email") or "?"
        names.append(nm[:30])
    suffix = f", ...and {len(attendees) - 8} more" if len(attendees) > 8 else ""
    return f"{len(attendees)} attendees: {', '.join(names)}{suffix}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--only-artem", action="store_true")
    ap.add_argument(
        "--regenerate-summary", action="store_true",
        help="After refreshing attendees, re-run short_summary so "
             "the «Участники:» line picks up the new list.",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)

    s = get_settings()
    cal_factory = build_calendar_credentials_factory_with_sa_fallback(s)
    if cal_factory is None:
        print("ERROR: no Calendar credentials available", file=sys.stderr)
        return 2

    from openai import OpenAI
    oc = OpenAI(api_key=s.openai_api_key)
    llm = OpenAIBackend(client=oc, model=s.fireflies_summary_model)

    with session_scope() as session:
        zoom_rows = [
            r for r in session.query(ZoomRecording).filter(
                ZoomRecording.meeting_date >= start,
                ZoomRecording.meeting_date < end,
            ).order_by(ZoomRecording.meeting_date).all()
            if (r.short_summary or "").strip()
            and (not args.only_artem or _has_artem(r.short_summary))
        ]
        ff_rows = [
            r for r in session.query(MeetingRecording).filter(
                MeetingRecording.meeting_date >= start,
                MeetingRecording.meeting_date < end,
            ).order_by(MeetingRecording.meeting_date).all()
            if (r.short_summary or "").strip()
            and (not args.only_artem or _has_artem(r.short_summary))
        ]
        all_rows: list[tuple[str, object]] = [
            ("zoom", r) for r in zoom_rows
        ] + [("fireflies", r) for r in ff_rows]
        all_rows.sort(key=lambda x: x[1].meeting_date)

        print(f"Refreshing {len(all_rows)} records.")
        for i, (src, r) in enumerate(all_rows, start=1):
            print()
            print(
                f"=== [{i}/{len(all_rows)}] "
                f"{r.meeting_date.strftime('%m-%d %H:%M')} "
                f"[{src}] {(r.title or '')[:55]} ==="
            )
            before = r.calendar_attendees or []
            print(f"  BEFORE: {_attendees_summary(before)}")

            # Re-run calendar attendees populate
            try:
                if src == "zoom":
                    from app.zoom.pipeline import ZoomPipeline
                    pipe = ZoomPipeline(
                        settings=s, llm_backend=llm,
                        calendar_factory=cal_factory,
                    )
                    pipe._populate_calendar_attendees(r, session)
                else:
                    from app.fireflies.pipeline import FirefliesPipeline
                    pipe = FirefliesPipeline(
                        settings=s, llm_backend=llm,
                        calendar_factory=cal_factory,
                    )
                    pipe._populate_calendar_attendees(r, session)
            except Exception as e:  # noqa: BLE001
                print(f"  ERR: {e}")
                continue

            after = r.calendar_attendees or []
            print(f"  AFTER:  {_attendees_summary(after)}")
            if len(after) != len(before):
                print(f"  DIFF:   {len(before)} → {len(after)}")

            if args.regenerate_summary and after:
                # Clear short_summary so pipeline regenerates it with
                # the fresh calendar_attendees list.
                if not args.dry_run:
                    r.short_summary = None
                    try:
                        ok = pipe._step_short_summary(session, r)
                    except Exception as e:  # noqa: BLE001
                        print(f"  short_summary ERR: {e}")
                        ok = False
                    print(
                        f"  short_summary regenerated: "
                        f"{'ok' if ok else 'failed'}"
                    )
            if not args.dry_run:
                session.flush()
        if not args.dry_run:
            session.commit()
            print("\nCOMMITTED.")
        else:
            print("\n--dry-run, no commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
