"""Read-only PREVIEW of what a processed meeting will surface — sends NOTHING.

Prints, per meeting (Zoom and/or Fireflies):
  • participants resolved from Google Calendar (row.calendar_attendees);
  • the long-summary Google Doc link;
  • the short summary text (as it would be posted);
  • the extracted tasks: owner | deadline | priority | direction, with the
    strategic ones (DIRECTIONS_IMPORTANT) flagged.

Use this to eyeball the output BEFORE enabling / before an auto-send, so you
can confirm participants come from Calendar (not LLM guesses), owners map to
real team members, deadlines are set, and the strategic filter is right.

Usage:
    docker exec manager-zoom-ff-1 python -m ops.meeting_preview --zoom-id <uuid>
    docker exec manager-zoom-ff-1 python -m ops.meeting_preview --transcript-id <ff_id>
    docker exec manager-zoom-ff-1 python -m ops.meeting_preview --since 2026-05-25
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from app.db import session_scope
from app.models import MeetingRecording, ZoomRecording
from app.models.task import Task, TaskSourceKind
from app.services.task_direction import DIRECTIONS_IMPORTANT


def _attendees(row) -> list[str]:
    out: list[str] = []
    for a in (row.calendar_attendees or []):
        if not isinstance(a, dict):
            continue
        name = (a.get("resolved_name") or "").strip()
        src = (a.get("source") or "").strip()
        email = (a.get("email") or "").strip()
        label = name or email or "?"
        if src:
            label += f" [{src}]"
        out.append(label)
    return out


def _tasks(session, *, source_kind, conversation_id) -> list[Task]:
    return (
        session.query(Task)
        .filter(Task.source_kind == source_kind)
        .filter(Task.source_conversation_id == conversation_id)
        .filter(Task.deleted_at.is_(None))
        .order_by(Task.id.asc())
        .all()
    )


def _deadline(t: Task) -> str:
    if not t.due_date:
        return "—"
    s = t.due_date.isoformat()
    if t.due_time:
        s += " " + t.due_time.strftime("%H:%M")
    return s


def _print_recording(session, *, kind: str, row, conv_id: str) -> None:
    source_kind = TaskSourceKind.zoom if kind == "zoom" else TaskSourceKind.fireflies
    when = row.meeting_date.strftime("%Y-%m-%d %H:%M") if row.meeting_date else "?"
    print("=" * 78)
    print(f"[{kind}] {row.title or '(без названия)'}   ({when})   id={conv_id}")
    print("-" * 78)

    attendees = _attendees(row)
    print("Участники (из Google Calendar):")
    if attendees:
        for a in attendees:
            print(f"  • {a}")
    else:
        print("  (пусто — Calendar не дал участников; участники будут из LLM-фолбэка)")

    print()
    print(f"Длинное саммери (Google Doc): {row.google_doc_url or '— (док не выгружен)'}")
    print(f"Транскрипт: {len(row.transcript_text or '')} симв.   "
          f"детальное саммери: {len(row.detailed_summary or '')} симв.")

    print()
    print("Короткое саммери (как уйдёт в Slack/TG):")
    short = (row.short_summary or "").strip()
    print(short if short else "  (ещё не сгенерировано)")

    tasks = _tasks(session, source_kind=source_kind, conversation_id=conv_id)
    strategic = [
        t for t in tasks
        if (isinstance(t.extra, dict) and (t.extra.get("direction") or "").strip().lower() in DIRECTIONS_IMPORTANT)
    ]
    print()
    print(f"Задачи: {len(tasks)} всего, из них стратегических: {len(strategic)}")
    print(f"  {'★':1} | {'Направление':12} | {'Приоритет':9} | {'Дедлайн':16} | {'Владелец':22} | Задача")
    for t in tasks:
        direction = (t.extra or {}).get("direction", "") if isinstance(t.extra, dict) else ""
        is_strat = (direction or "").strip().lower() in DIRECTIONS_IMPORTANT
        prio = t.priority.value if t.priority else "—"
        print(f"  {'★' if is_strat else ' '} | {direction[:12]:12} | {prio:9} | "
              f"{_deadline(t):16} | {(t.owner_display_name or '—')[:22]:22} | {(t.title or '')[:60]}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zoom-id", default="")
    ap.add_argument("--transcript-id", default="", help="Fireflies transcript id.")
    ap.add_argument("--since", default="", help="ISO date — preview всех встреч с этой даты.")
    ap.add_argument("--until", default="", help="ISO date (исключая).")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    if not (args.zoom_id or args.transcript_id or args.since):
        print("Укажи --zoom-id, --transcript-id или --since", file=sys.stderr)
        return 2

    with session_scope() as session:
        if args.zoom_id:
            row = session.query(ZoomRecording).filter(
                ZoomRecording.zoom_id == args.zoom_id
            ).one_or_none()
            if row is None:
                print(f"Zoom-запись {args.zoom_id!r} не найдена", file=sys.stderr)
                return 3
            _print_recording(session, kind="zoom", row=row, conv_id=row.zoom_id)
        if args.transcript_id:
            row = session.query(MeetingRecording).filter(
                MeetingRecording.fireflies_id == args.transcript_id
            ).one_or_none()
            if row is None:
                print(f"Fireflies-запись {args.transcript_id!r} не найдена", file=sys.stderr)
                return 3
            _print_recording(session, kind="fireflies", row=row, conv_id=row.fireflies_id)
        if args.since:
            since_dt = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
            until_dt = (
                datetime.fromisoformat(args.until).replace(tzinfo=timezone.utc)
                if args.until else datetime.now(timezone.utc)
            )
            zooms = session.query(ZoomRecording).filter(
                ZoomRecording.meeting_date >= since_dt,
                ZoomRecording.meeting_date < until_dt,
            ).order_by(ZoomRecording.meeting_date).limit(args.limit).all()
            ffs = session.query(MeetingRecording).filter(
                MeetingRecording.meeting_date >= since_dt,
                MeetingRecording.meeting_date < until_dt,
            ).order_by(MeetingRecording.meeting_date).limit(args.limit).all()
            print(f"Найдено: {len(zooms)} Zoom + {len(ffs)} Fireflies (с {args.since})\n")
            for r in zooms:
                _print_recording(session, kind="zoom", row=r, conv_id=r.zoom_id)
            for r in ffs:
                _print_recording(session, kind="fireflies", row=r, conv_id=r.fireflies_id)

        session.rollback()  # READ-ONLY: ничего не пишем
    return 0


if __name__ == "__main__":
    sys.exit(main())
