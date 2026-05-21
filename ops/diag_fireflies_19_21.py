"""Диагностика Fireflies для окна 19-21 мая 2026.

  1. Что отдаёт API (`list_transcripts(limit=200)`) — сырые ids/даты.
  2. Что у нас в БД (`MeetingRecording`) для того же окна.
  3. Расхождение: что есть в API но нет в БД (новые/пропущенные),
     и наоборот.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.config import get_settings
from app.db import session_scope
from app.fireflies.client import FirefliesClient
from app.models import MeetingRecording


def main() -> int:
    s = get_settings()
    start = datetime(2026, 5, 19, tzinfo=timezone.utc)
    end = datetime(2026, 5, 22, tzinfo=timezone.utc)

    print("\n## 1. Fireflies API: list_transcripts(limit=200)")
    ff = FirefliesClient(
        token=s.fireflies_api_token,
        endpoint=s.fireflies_api_url,
    )
    if not ff.enabled:
        print("   ERROR: Fireflies API not enabled (no token / endpoint).")
    else:
        ts = ff.list_transcripts(limit=200, skip=0)
        print(f"   API вернул {len(ts)} последних.")
        in_range = [t for t in ts if t.meeting_date and start <= t.meeting_date < end]
        print(f"   В окне 19-21 мая: {len(in_range)}")
        if ts:
            print(f"   Самая свежая: {ts[0].meeting_date} «{ts[0].title}»")
            print(f"   Самая старая: {ts[-1].meeting_date} «{ts[-1].title}»")
        print()
        for t in in_range:
            print(
                f"   {t.meeting_date.strftime('%m-%d %H:%M')}  "
                f"{(t.title or '')[:55]:<55}  id={t.id}"
            )

    print("\n## 2. БД: MeetingRecording в окне")
    with session_scope() as session:
        rows = session.query(MeetingRecording).filter(
            MeetingRecording.meeting_date >= start,
            MeetingRecording.meeting_date < end,
        ).order_by(MeetingRecording.meeting_date.asc()).all()
        print(f"   В БД: {len(rows)}")
        for r in rows:
            txt = len(r.transcript_text or "") if r.transcript_text else 0
            print(
                f"   {r.meeting_date.strftime('%m-%d %H:%M')}  "
                f"{(r.title or '')[:55]:<55}  "
                f"txt={txt//1000}k  ff_id={r.fireflies_id}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
