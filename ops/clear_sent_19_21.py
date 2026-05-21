"""Operator-pinned 2026-05-21 — clear `short_summary_sent=False` for
records 19-21 May that have non-empty `short_summary` content.

Background (FR-CR-05-190): `reprocess_zoom_summary` and
`reprocess_fireflies_summary` implement `--no-slack` by pre-setting
`short_summary_sent=True` before running the pipeline (so the
pipeline's send step short-circuits). Side effect: after batch
reprocess, all records sit at sent=True, which makes
`send_batch_chronological` skip them.

This script clears sent=False so `send_batch_chronological` can pick
them up. Safe to run multiple times.

Filter:
  - meeting_date in [start, end)
  - non-empty short_summary (skip records where reprocess failed)
  - non-empty google_doc_url (extra safety — won't unset records
    that don't have a doc to link to)

Usage:
    docker exec manager-bot-1 python -m ops.clear_sent_19_21 \\
        --start 2026-05-19 --end 2026-05-22
    docker exec manager-bot-1 python -m ops.clear_sent_19_21 \\
        --start 2026-05-19 --end 2026-05-22 --dry-run
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from app.db import session_scope
from app.models import MeetingRecording, ZoomRecording


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Print what would change, don't commit.",
    )
    ap.add_argument(
        "--skip-zoom", action="store_true",
        help="Skip Zoom records.",
    )
    ap.add_argument(
        "--skip-fireflies", action="store_true",
        help="Skip Fireflies records.",
    )
    args = ap.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)

    flipped = 0
    skipped_empty = 0
    skipped_already_false = 0
    with session_scope() as s:
        if not args.skip_zoom:
            rows = s.query(ZoomRecording).filter(
                ZoomRecording.meeting_date >= start,
                ZoomRecording.meeting_date < end,
            ).all()
            for r in rows:
                short = (r.short_summary or "").strip()
                doc = (r.google_doc_url or "").strip()
                if not short or not doc:
                    skipped_empty += 1
                    continue
                if not r.short_summary_sent:
                    skipped_already_false += 1
                    continue
                print(
                    f"  zoom {r.meeting_date.strftime('%m-%d %H:%M')} "
                    f"| {(r.title or '')[:55]}"
                )
                if not args.dry_run:
                    r.short_summary_sent = False
                flipped += 1
        if not args.skip_fireflies:
            rows = s.query(MeetingRecording).filter(
                MeetingRecording.meeting_date >= start,
                MeetingRecording.meeting_date < end,
            ).all()
            for r in rows:
                short = (r.short_summary or "").strip()
                doc = (r.google_doc_url or "").strip()
                if not short or not doc:
                    skipped_empty += 1
                    continue
                if not r.short_summary_sent:
                    skipped_already_false += 1
                    continue
                print(
                    f"  ff   {r.meeting_date.strftime('%m-%d %H:%M')} "
                    f"| {(r.title or '')[:55]}"
                )
                if not args.dry_run:
                    r.short_summary_sent = False
                flipped += 1
        if not args.dry_run:
            s.commit()

    print()
    print(f"Flipped to sent=False: {flipped}")
    print(f"Skipped (empty content): {skipped_empty}")
    print(f"Skipped (already sent=False): {skipped_already_false}")
    if args.dry_run:
        print("\n--dry-run — no commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
