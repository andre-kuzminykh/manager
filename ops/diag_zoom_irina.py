"""Diag: для каждой записи с title содержащим «Ирина» в окне
19-21 мая показать zoom_id, meeting_date, zoom_meeting_id,
audio_url присутствие, transcript_text присутствие — чтобы понять
почему не все попали в bilingual батч."""
from __future__ import annotations

from datetime import datetime, timezone

from app.db import session_scope
from app.models import ZoomRecording


def main() -> int:
    start = datetime(2026, 5, 19, tzinfo=timezone.utc)
    end = datetime(2026, 5, 22, tzinfo=timezone.utc)
    with session_scope() as session:
        rows = session.query(ZoomRecording).filter(
            ZoomRecording.meeting_date >= start,
            ZoomRecording.meeting_date < end,
        ).order_by(ZoomRecording.meeting_date.asc()).all()
        print(f"{'date':<18} {'zoom_id':<28} {'meeting_id':<14} "
              f"{'audio?':<7} {'txt?':<6} {'host_email':<26} title")
        print("-" * 140)
        for r in rows:
            t = (r.title or "")
            if "Ирин" not in t and "Irin" not in t:
                continue
            dt = r.meeting_date.strftime("%Y-%m-%d %H:%M") if r.meeting_date else "?"
            mid = (r.zoom_meeting_id or "")[:13]
            has_aud = "Y" if r.audio_url else "N"
            has_txt = "Y" if r.transcript_text else "N"
            host = (r.host_email or "")[:25]
            print(f"{dt:<18} {r.zoom_id:<28} {mid:<14} "
                  f"{has_aud:<7} {has_txt:<6} {host:<26} {t[:50]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
