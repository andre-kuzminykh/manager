"""Operator-pinned 2026-05-21 — preview-table перед батчевой
отправкой саммари за 19-21 мая в Slack.

Объединяет Zoom + Fireflies в один список, фильтрует по:
  - duration ≥ 10 мин
  - meeting_date в окне 19-21 мая
  - есть в БД (или хотя бы в API)

Для каждой строки показывает:
  - source       : zoom | fireflies
  - дата/время   : meeting_date в UTC
  - длительность : duration_seconds → mm:ss
  - title        : коротко
  - txt          : длина transcript_text в k-chars
  - summary?     : есть/нет short_summary (готов к отправке)
  - doc?         : есть/нет google_doc_url
  - tasks        : сколько Task с source_conversation_id == record id
  - status       : READY | NEEDS_REPROCESS | DONE

READY        — short_summary + google_doc_url есть, не отправлено в Slack
NEEDS_REPROC — нет транскрипта/саммари/Doc (нужно reprocess --force-retranscribe --no-slack)
DONE         — short_summary_sent=True (уже отправлено, пропустить)

Read-only. Никаких изменений в БД.

Usage:
    docker compose exec -T bot python -m ops.preview_summaries_19_21 \\
        --start 2026-05-19 --end 2026-05-22 --min-minutes 10
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from app.db import session_scope
from app.models import MeetingRecording, Task, TaskSourceKind, ZoomRecording


def _fmt_duration(secs: int | None) -> str:
    if not secs:
        return "?"
    m, s = divmod(int(secs), 60)
    return f"{m:>2}:{s:02d}"


def _status(row, has_tasks: int) -> str:
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
    ap.add_argument("--start", required=True, help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD (exclusive)")
    ap.add_argument(
        "--min-minutes", type=int, default=10,
        help="Drop recordings shorter than this (default 10).",
    )
    args = ap.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    min_secs = args.min_minutes * 60

    rows: list[tuple[datetime, str, object]] = []
    with session_scope() as session:
        z_rows = session.query(ZoomRecording).filter(
            ZoomRecording.meeting_date >= start,
            ZoomRecording.meeting_date < end,
        ).all()
        for r in z_rows:
            if (r.duration_seconds or 0) < min_secs:
                continue
            rows.append((r.meeting_date, "zoom", r))

        f_rows = session.query(MeetingRecording).filter(
            MeetingRecording.meeting_date >= start,
            MeetingRecording.meeting_date < end,
        ).all()
        for r in f_rows:
            # FR-CR-05-188: FF может не заполнять duration_seconds.
            # Фильтр по duration используем только если поле > 0.
            # Иначе требуем хотя бы непустой transcript_text > 1500 chars
            # (≈ 2 минуты речи) — отсекает audio_*.ogg войс-нотки.
            dur = r.duration_seconds or 0
            if dur > 0 and dur < min_secs:
                continue
            if dur == 0 and len((r.transcript_text or "").strip()) < 1500:
                continue
            rows.append((r.meeting_date, "fireflies", r))

        rows.sort(key=lambda x: x[0])

        # Considering tasks: query once and bucket by source_id.
        zoom_ids = [r.zoom_id for (_dt, src, r) in rows if src == "zoom"]
        ff_ids = [r.fireflies_id for (_dt, src, r) in rows if src == "fireflies"]
        task_counts: dict[tuple[str, str], int] = {}
        if zoom_ids:
            zt = session.query(
                Task.source_conversation_id,
            ).filter(
                Task.source_kind == TaskSourceKind.zoom,
                Task.source_conversation_id.in_(zoom_ids),
            ).all()
            for (sid,) in zt:
                task_counts[("zoom", sid)] = task_counts.get(("zoom", sid), 0) + 1
        if ff_ids:
            ft = session.query(
                Task.source_conversation_id,
            ).filter(
                Task.source_kind == TaskSourceKind.fireflies,
                Task.source_conversation_id.in_(ff_ids),
            ).all()
            for (sid,) in ft:
                task_counts[("fireflies", sid)] = (
                    task_counts.get(("fireflies", sid), 0) + 1
                )

        print(f"\n# Preview {args.start} .. {args.end} "
              f"(≥ {args.min_minutes} min)")
        print(
            f"\n{'#':<3} {'date':<14} {'dur':<6} {'src':<10} "
            f"{'txt':<5} {'sum?':<5} {'doc?':<5} {'tasks':<5} "
            f"{'status':<14} title"
        )
        print("-" * 130)
        for i, (dt, src, r) in enumerate(rows, start=1):
            sid = r.zoom_id if src == "zoom" else r.fireflies_id
            tcount = task_counts.get((src, sid), 0)
            stat = _status(r, tcount)
            txt = len(r.transcript_text or "")
            print(
                f"{i:<3} {dt.strftime('%m-%d %H:%M'):<14} "
                f"{_fmt_duration(r.duration_seconds):<6} "
                f"{src:<10} {txt//1000:>3}k  "
                f"{('Y' if (r.short_summary or '').strip() else '—'):<5} "
                f"{('Y' if (r.google_doc_url or '').strip() else '—'):<5} "
                f"{tcount:<5} {stat:<14} {(r.title or '')[:55]}"
            )

        # Roll-up
        ready = sum(1 for (_dt, src, r) in rows
                    if _status(r, 0) == "READY")
        need = sum(1 for (_dt, src, r) in rows
                   if _status(r, 0) == "NEEDS_REPROC")
        done = sum(1 for (_dt, src, r) in rows
                   if _status(r, 0) == "DONE")
        print("-" * 130)
        print(
            f"\nTotal: {len(rows)}   READY: {ready}   "
            f"NEEDS_REPROC: {need}   DONE: {done}"
        )
        if need:
            print(
                "\nNext: run batch reprocess (--force-retranscribe "
                "--no-slack) на все NEEDS_REPROC. Команда:\n"
                "  python -m ops.batch_reprocess_19_21 "
                "--start 2026-05-19 --end 2026-05-22"
            )
        if ready:
            print(
                "\nAfter approval: post READY-rows в Slack в "
                "хронопорядке. Команда:\n"
                "  python -m ops.send_summaries_19_21 "
                "--start 2026-05-19 --end 2026-05-22"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
