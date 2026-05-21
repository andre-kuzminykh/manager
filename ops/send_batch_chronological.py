"""Send ALL prepared meeting summaries (Zoom + Fireflies) to a Slack
channel in chronological order (oldest → newest). Skips rows that
aren't READY (no short_summary / no google_doc_url / already sent).

Wraps `ops.send_one_zoom` and `ops.send_one_fireflies` — same parent
+ thread Slack format, same defensive Task DELETE.

Operator-pinned 2026-05-21: «потом еще подберем firefiles в таком
же формате и в правильном хронологическом порядке опубликуем».

Usage:
    docker compose exec -T bot python -m ops.send_batch_chronological \\
        --start 2026-05-19 --end 2026-05-22 --min-minutes 10 \\
        --channel D0ASY5QF6UX --dry-run

    # real:
    docker compose exec -T bot python -m ops.send_batch_chronological \\
        --start 2026-05-19 --end 2026-05-22 --min-minutes 10 \\
        --channel D0ASY5QF6UX --yes
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone

from app.db import session_scope
from app.models import MeetingRecording, ZoomRecording


def _ready(row) -> bool:
    return (
        not row.short_summary_sent
        and bool((row.short_summary or "").strip())
        and bool((row.google_doc_url or "").strip())
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--min-minutes", type=int, default=10)
    ap.add_argument("--channel", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument(
        "--sleep", type=float, default=2.0,
        help="Seconds between Slack posts (rate-limit cushion).",
    )
    ap.add_argument(
        "--skip-zoom", action="store_true",
        help="Only Fireflies records.",
    )
    ap.add_argument(
        "--skip-fireflies", action="store_true",
        help="Only Zoom records.",
    )
    ap.add_argument(
        "--exclude-title-contains", action="append", default=[],
        help=(
            "Drop records whose title contains this substring "
            "(case-insensitive). Repeatable."
        ),
    )
    args = ap.parse_args()
    excludes = [s.lower() for s in (args.exclude_title_contains or []) if s]

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    min_secs = args.min_minutes * 60

    candidates: list[tuple[datetime, str, str, str]] = []
    with session_scope() as session:
        if not args.skip_zoom:
            for r in session.query(ZoomRecording).filter(
                ZoomRecording.meeting_date >= start,
                ZoomRecording.meeting_date < end,
            ).all():
                if (r.duration_seconds or 0) < min_secs:
                    continue
                if not _ready(r):
                    continue
                title = r.title or ""
                if any(e in title.lower() for e in excludes):
                    continue
                candidates.append(
                    (r.meeting_date, "zoom", r.zoom_id, title)
                )
        if not args.skip_fireflies:
            for r in session.query(MeetingRecording).filter(
                MeetingRecording.meeting_date >= start,
                MeetingRecording.meeting_date < end,
            ).all():
                # FR-CR-05-188: FF может не заполнять duration_seconds.
                dur = r.duration_seconds or 0
                if dur > 0 and dur < min_secs:
                    continue
                if dur == 0 and len((r.transcript_text or "").strip()) < 1500:
                    continue
                if not _ready(r):
                    continue
                title = r.title or ""
                if any(e in title.lower() for e in excludes):
                    continue
                candidates.append(
                    (
                        r.meeting_date, "fireflies", r.fireflies_id,
                        title,
                    )
                )

    candidates.sort(key=lambda x: x[0])
    print(
        f"\n# Chronological send plan: {len(candidates)} READY records "
        f"({args.start} .. {args.end}, ≥ {args.min_minutes} min)"
    )
    print(f"\n{'#':<3} {'date':<14} {'src':<10} title")
    print("-" * 100)
    for i, (dt, src, rid, title) in enumerate(candidates, start=1):
        print(
            f"{i:<3} {dt.strftime('%m-%d %H:%M'):<14} {src:<10} "
            f"{title[:60]}"
        )
    print("-" * 100)
    if not candidates:
        print("Nothing to send.")
        return 0

    if args.dry_run:
        print("\n--dry-run, not posting. Pass --yes to send.")
        return 0
    if not args.yes:
        ans = input("\nProceed and post to Slack in this order? [yes/N]: ")
        if ans.strip().lower() != "yes":
            print("Aborted.")
            return 1

    failures: list[tuple[str, str, str]] = []
    sent = 0
    for i, (dt, src, rid, title) in enumerate(candidates, start=1):
        print(
            f"\n=== [{i}/{len(candidates)}] {dt.strftime('%m-%d %H:%M')} "
            f"{src} | {title[:50]} ==="
        )
        if src == "zoom":
            cmd = [
                "python", "-m", "ops.send_one_zoom",
                "--zoom-id", rid,
                "--channel", args.channel,
            ]
        else:
            cmd = [
                "python", "-m", "ops.send_one_fireflies",
                "--fireflies-id", rid,
                "--channel", args.channel,
            ]
        try:
            proc = subprocess.run(cmd, capture_output=False, timeout=300)
            if proc.returncode != 0:
                failures.append((rid, title, f"rc={proc.returncode}"))
            else:
                sent += 1
        except subprocess.TimeoutExpired:
            failures.append((rid, title, "timeout"))
        except Exception as e:  # noqa: BLE001
            failures.append((rid, title, str(e)))
        time.sleep(args.sleep)

    print("\n" + "=" * 70)
    print(
        f"Sent: {sent}/{len(candidates)}   Failed: {len(failures)}"
    )
    if failures:
        print("\nFailures:")
        for rid, title, err in failures:
            print(f"  [{err}] {rid} {title}")
    return 0 if not failures else 4


if __name__ == "__main__":
    sys.exit(main())
