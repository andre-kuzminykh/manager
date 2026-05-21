"""Diag: для конкретного zoom_id показать ОТКУДА берутся участники
для summary. Сравнивает:
  - row.calendar_attendees  (FR-CR-05-169 — calendar event invitees,
    resolved через People/Counterparty)
  - row.participants        (FR-CR-05-139 — LLM-extracted из транскрипта)
  - row.host_email          (Zoom-side host)
  - что вернёт `build_meta_block_for_summary(row)` — финальный block
    который попадёт в LLM detailed_summary prompt.

Usage:
    docker compose exec -T bot python -m ops.diag_zoom_participants \\
        --zoom-id "..."
    OR
    docker compose exec -T bot python -m ops.diag_zoom_participants \\
        --date 2026-05-21 --hour 9 --title-contains Fundraising
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

from app.db import session_scope
from app.models import ZoomRecording
from app.zoom.pipeline import build_meta_block_for_summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zoom-id", default=None)
    ap.add_argument("--date", default=None, help="YYYY-MM-DD")
    ap.add_argument("--hour", type=int, default=None)
    ap.add_argument("--title-contains", default=None)
    args = ap.parse_args()

    with session_scope() as session:
        q = session.query(ZoomRecording)
        if args.zoom_id:
            q = q.filter(ZoomRecording.zoom_id == args.zoom_id)
        else:
            if args.date:
                d0 = datetime.fromisoformat(args.date).replace(
                    tzinfo=timezone.utc
                )
                d1 = d0 + timedelta(days=1)
                if args.hour is not None:
                    d0 = d0.replace(hour=args.hour)
                    d1 = d0 + timedelta(hours=2)
                q = q.filter(
                    ZoomRecording.meeting_date >= d0,
                    ZoomRecording.meeting_date < d1,
                )
            if args.title_contains:
                q = q.filter(
                    ZoomRecording.title.ilike(f"%{args.title_contains}%")
                )
        rows = q.order_by(ZoomRecording.meeting_date.asc()).all()
        if not rows:
            print("NO ROWS MATCHED.")
            return 1
        for r in rows:
            print("\n" + "=" * 78)
            print(f"zoom_id:        {r.zoom_id}")
            print(f"title:          {r.title!r}")
            print(f"meeting_date:   {r.meeting_date}")
            print(f"duration_secs:  {r.duration_seconds}")
            print(f"host_email:     {r.host_email!r}")
            print(f"transcribed:    {r.transcribed}  txt_chars="
                  f"{len(r.transcript_text or '')}")
            print(f"detailed_sum:   {r.detailed_summarised}  "
                  f"chars={len(r.detailed_summary or '')}")
            print(f"google_doc_url: {r.google_doc_url}")
            print()
            print("--- row.calendar_attendees (FR-CR-05-169 / 172) ---")
            ca = r.calendar_attendees or []
            if not ca:
                print("  (empty)")
            else:
                for a in ca:
                    if isinstance(a, dict):
                        print(
                            f"  • resolved={a.get('resolved_name')!r:<35} "
                            f"display={a.get('display_name')!r:<25} "
                            f"email={a.get('email')!r:<35} "
                            f"source={a.get('source')!r:<14} "
                            f"status={a.get('response_status')!r}"
                        )
                    else:
                        print(f"  • {a!r}")
            print()
            print("--- row.participants (FR-CR-05-139 — LLM-from-transcript) ---")
            for p in (r.participants or []):
                print(f"  • {p!r}")
            print()
            print("--- build_meta_block_for_summary(row) — что идёт в LLM ---")
            print(build_meta_block_for_summary(r))
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
