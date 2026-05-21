"""Operator-pinned 2026-05-21 — batch reprocess Zoom + Fireflies
recordings 19-21 мая. Прогоняет `reprocess_zoom_summary` для каждой
Zoom-записи и `reprocess_fireflies_summary` для каждой Fireflies-записи,
ВСЕГДА с ``--no-slack`` (Slack-отправка — отдельный шаг через
``send_summaries_19_21``).

Перед прогоном выводит таблицу (через ``preview_summaries_19_21``-логику)
и спрашивает подтверждение. ``--yes`` пропускает интерактив.

Usage:
    docker compose exec -T bot python -m ops.batch_reprocess_19_21 \\
        --start 2026-05-19 --end 2026-05-22 --min-minutes 10
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone

from app.db import session_scope
from app.models import MeetingRecording, ZoomRecording


def _status_for(row, source: str) -> str:
    if row.short_summary_sent:
        return "DONE"
    if not (row.transcript_text or "").strip():
        return "NEEDS_REPROC"
    if not (row.detailed_summary or "").strip():
        return "NEEDS_REPROC"
    if not (row.short_summary or "").strip():
        return "NEEDS_REPROC"
    if not (row.google_doc_url or "").strip():
        return "NEEDS_REPROC"
    return "READY"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--min-minutes", type=int, default=10)
    ap.add_argument(
        "--yes", action="store_true",
        help="Skip interactive confirmation.",
    )
    ap.add_argument(
        "--skip-zoom", action="store_true",
        help="Skip Zoom reprocess (only Fireflies).",
    )
    ap.add_argument(
        "--skip-fireflies", action="store_true",
        help="Skip Fireflies reprocess (only Zoom).",
    )
    ap.add_argument(
        "--force-all", action="store_true",
        help="Reprocess READY rows too. Default skips them.",
    )
    args = ap.parse_args()

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
                st = _status_for(r, "zoom")
                if st == "DONE":
                    continue
                if st == "READY" and not args.force_all:
                    continue
                candidates.append(
                    (r.meeting_date, "zoom", r.zoom_id, r.title or "")
                )
        if not args.skip_fireflies:
            for r in session.query(MeetingRecording).filter(
                MeetingRecording.meeting_date >= start,
                MeetingRecording.meeting_date < end,
            ).all():
                if (r.duration_seconds or 0) < min_secs:
                    continue
                st = _status_for(r, "fireflies")
                if st == "DONE":
                    continue
                if st == "READY" and not args.force_all:
                    continue
                candidates.append(
                    (r.meeting_date, "fireflies", r.fireflies_id, r.title or "")
                )

    candidates.sort(key=lambda x: x[0])
    print(
        f"\n# Batch reprocess plan: {len(candidates)} records "
        f"({args.start} .. {args.end}, ≥ {args.min_minutes} min)"
    )
    print(f"{'#':<3} {'date':<14} {'src':<10} title")
    print("-" * 100)
    for i, (dt, src, rid, title) in enumerate(candidates, start=1):
        print(
            f"{i:<3} {dt.strftime('%m-%d %H:%M'):<14} {src:<10} "
            f"{title[:60]}"
        )
    print("-" * 100)
    if not candidates:
        print("Nothing to do.")
        return 0

    if not args.yes:
        ans = input("\nProceed? [yes/N]: ").strip().lower()
        if ans != "yes":
            print("Aborted.")
            return 1

    failures: list[tuple[str, str, str]] = []
    for i, (dt, src, rid, title) in enumerate(candidates, start=1):
        print(
            f"\n=== [{i}/{len(candidates)}] {dt.strftime('%m-%d %H:%M')} "
            f"{src} | {title[:50]} ==="
        )
        if src == "zoom":
            cmd = [
                "python", "-m", "ops.reprocess_zoom_summary",
                "--zoom-id", rid,
                "--force-retranscribe",
                "--no-slack",
                # FR-CR-05-178 — mock mode: no Task rows persisted.
                # send_summaries_19_21 will extract tasks ephemerally.
                "--skip-tasks",
            ]
        else:
            cmd = [
                "python", "-m", "ops.reprocess_fireflies_summary",
                "--fireflies-id", rid,
                "--no-slack",
            ]
        try:
            proc = subprocess.run(cmd, capture_output=False, timeout=1800)
            if proc.returncode != 0:
                failures.append((rid, title, f"rc={proc.returncode}"))
        except subprocess.TimeoutExpired:
            failures.append((rid, title, "timeout"))
        except Exception as e:  # noqa: BLE001
            failures.append((rid, title, str(e)))

    print("\n" + "=" * 70)
    print(
        f"Batch done. OK: {len(candidates) - len(failures)}   "
        f"Failed: {len(failures)}"
    )
    if failures:
        print("\nFailures:")
        for rid, title, err in failures:
            print(f"  [{err}] {rid} {title}")
    print(
        "\nNext: run preview again to see READY-rows; then "
        "send_summaries_19_21 to post to Slack."
    )
    return 0 if not failures else 4


if __name__ == "__main__":
    sys.exit(main())
