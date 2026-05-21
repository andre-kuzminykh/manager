"""FR-CR-05-192 debug — list ALL Calendar events in a ±2h window
around a specific meeting, so we can see which event the
calendar_attendees matcher picked vs which event should have been
matched.

Usage:
    docker exec manager-bot-1 python -m ops.debug_calendar_window \\
        --zoom-id "k9We5mXQRsy3aiv5rOOsHg=="

    # OR by fireflies_id
    docker exec manager-bot-1 python -m ops.debug_calendar_window \\
        --fireflies-id "01KS0DR2120VV8T352W9CMRRB5"
"""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta

from app.config import get_settings
from app.db import session_scope
from app.models import MeetingRecording, ZoomRecording
from app.services.calendar_match import fetch_calendar_events_via_api
from app.sync.factories import (
    build_calendar_credentials_factory_with_sa_fallback,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zoom-id", default=None)
    ap.add_argument("--fireflies-id", default=None)
    ap.add_argument(
        "--window-min", type=int, default=120,
        help="Window ± minutes around meeting_date (default 120).",
    )
    args = ap.parse_args()
    if not args.zoom_id and not args.fireflies_id:
        print("ERROR: --zoom-id or --fireflies-id required",
              file=sys.stderr)
        return 1

    s = get_settings()
    cal_factory = build_calendar_credentials_factory_with_sa_fallback(s)
    if cal_factory is None:
        print("ERROR: Calendar credentials not available", file=sys.stderr)
        return 2

    with session_scope() as session:
        if args.zoom_id:
            r = session.query(ZoomRecording).filter(
                ZoomRecording.zoom_id == args.zoom_id,
            ).first()
        else:
            r = session.query(MeetingRecording).filter(
                MeetingRecording.fireflies_id == args.fireflies_id,
            ).first()
        if r is None:
            print("Record not found", file=sys.stderr)
            return 3

        md = r.meeting_date
        win_start = md - timedelta(minutes=args.window_min)
        win_end = md + timedelta(minutes=args.window_min)
        print(f"\nRecord: {r.title}")
        print(f"meeting_date: {md.isoformat()}")
        print(
            f"window: {win_start.isoformat()} "
            f"→ {win_end.isoformat()} "
            f"(±{args.window_min} min)"
        )
        print(f"google_calendar_id: {s.google_calendar_id}")
        print()

        # google_calendar_id can be comma-separated list of calendars
        calendar_ids = [
            c.strip() for c in (s.google_calendar_id or "primary").split(",")
            if c.strip()
        ]
        events: list = []
        for cid in calendar_ids:
            try:
                ev = fetch_calendar_events_via_api(
                    md,
                    window_minutes=args.window_min,
                    credentials_factory=cal_factory,
                    calendar_id=cid,
                )
                print(f"Calendar {cid[:50]}: {len(ev)} events")
                events.extend(ev)
            except Exception as e:  # noqa: BLE001
                print(f"Calendar {cid[:50]}: ERR {e}")
        print()
        print(f"Total events across all calendars: {len(events)}")
        print()
        for ev in events:
            ev_id = ev.get("id") or "?"
            # fetch_calendar_events_via_api returns shape with
            # `title` (not `summary`) per FR-CR-05-144 normalisation
            summary = (
                ev.get("title") or ev.get("summary") or "?"
            )
            start = (
                ev.get("start") or (ev.get("start_raw") or {}).get("dateTime")
                or "?"
            )
            attendees = ev.get("attendees") or []
            atts_count = len(attendees)
            decl = sum(
                1 for a in attendees
                if (a.get("responseStatus") or "").lower() == "declined"
            )
            print(f"  -- {summary[:60]}")
            print(f"     id={ev_id}")
            print(f"     start={start}   attendees={atts_count} "
                  f"(declined={decl})")
            for a in attendees[:12]:
                em = a.get("email") or ""
                dn = a.get("displayName") or ""
                rs = a.get("responseStatus") or ""
                tag = " [DECLINED]" if rs == "declined" else ""
                resrc = a.get("resource") or False
                opt = a.get("optional") or False
                tags = []
                if resrc: tags.append("RESRC")
                if opt: tags.append("OPT")
                tag_str = (" [" + ",".join(tags) + "]") if tags else ""
                print(f"        · {(dn or em)[:30]:<30} {em:<35} "
                      f"rsvp={rs}{tag}{tag_str}")
            if atts_count > 12:
                print(f"        · ...and {atts_count - 12} more")
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
