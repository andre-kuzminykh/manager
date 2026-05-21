#!/usr/bin/env python3
"""Operator-pinned 2026-05-21: bilingual нужно прогнать по тем Zoom
записям, где в КАЛЕНДАРНОМ событии Артем либо среди attendees, либо
он organizer/creator (host). Старые записи имеют пустые
`row.calendar_attendees` / `row.host_email` — ходим в Calendar API
live для каждой строки.

Usage:
    docker compose exec -T bot python -m ops.filter_zoom_with_artem \\
        --start 2026-05-19 --end 2026-05-22 \\
        --required-email 1@thehumanoid.ai

Outputs:
  - per-row table (date | signal | match | title)
  - /tmp/zoom_ids_bilingual_with_artem.txt — список zoom_id для батча

Signal column:
  - calendar_attendee  — операторный email среди attendees
  - host_organizer     — operator == organizer.email
  - host_creator       — operator == creator.email
  - no_event_match     — Calendar API нашёл события в окне, но ни одно
                         не совпало по zoom_meeting_id URL / fuzzy
  - no_events_in_window — Calendar API вернул пустой список
  - —  SKIP            — match есть, но Artem отсутствует
"""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta, timezone
from datetime import datetime as _dt
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.db import session_scope
from app.models import ZoomRecording
from app.services.calendar_attendees import _find_matching_event
from app.services.calendar_match import fetch_calendar_events_via_api
from app.sync.factories import (
    build_calendar_credentials_factory_with_sa_fallback,
)


def _operator_in_event(ev: dict[str, Any], required: str) -> str | None:
    """Return which signal proves operator presence in this event,
    or None when no signal matches."""
    org = (ev.get("organizer") or {}).get("email", "").strip().lower()
    if org == required:
        return "host_organizer"
    cre = (ev.get("creator") or {}).get("email", "").strip().lower()
    if cre == required:
        return "host_creator"
    for a in (ev.get("attendees") or []):
        if not isinstance(a, dict):
            continue
        email = (a.get("email") or "").strip().lower()
        if email == required:
            # `responseStatus="declined"` — он отклонил, не пошёл
            if (a.get("responseStatus") or "").strip().lower() == "declined":
                continue
            return "calendar_attendee"
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD (exclusive)")
    ap.add_argument(
        "--out", default="/tmp/zoom_ids_bilingual_with_artem.txt",
        help="Output path for filtered zoom_ids",
    )
    ap.add_argument(
        "--required-email", default=None,
        help="Override ZOOM_REQUIRED_EMAIL (e.g. 1@thehumanoid.ai). "
        "Useful when the env var is unset in the running container.",
    )
    args = ap.parse_args()

    s = get_settings()
    required = (
        args.required_email or s.zoom_required_email or ""
    ).strip().lower()
    if not required:
        print(
            "ERROR: ZOOM_REQUIRED_EMAIL not set and --required-email "
            "not passed.", file=sys.stderr,
        )
        return 2

    cal_factory = build_calendar_credentials_factory_with_sa_fallback(s)
    if cal_factory is None:
        print(
            "ERROR: no Calendar credentials (SA or OAuth). Cannot "
            "resolve attendees/organizer.", file=sys.stderr,
        )
        return 3
    cal_id = s.google_calendar_id or "primary"
    window_min = 30

    start = _dt.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = _dt.fromisoformat(args.end).replace(tzinfo=timezone.utc)

    with session_scope() as session:
        rows = session.query(ZoomRecording).filter(
            ZoomRecording.meeting_date >= start,
            ZoomRecording.meeting_date < end,
            ZoomRecording.duration_seconds > 300,
            ZoomRecording.duration_seconds < 7200,
            ZoomRecording.audio_url.isnot(None),
            ZoomRecording.transcript_text.isnot(None),
        ).order_by(ZoomRecording.meeting_date.asc()).all()

        kept: list[str] = []
        skipped: list[tuple[str, str, str]] = []
        print(
            f"\n{'date':<18} {'signal':<18} {'match':<8} title"
        )
        print("-" * 110)
        for r in rows:
            events = fetch_calendar_events_via_api(
                r.meeting_date,
                window_minutes=window_min,
                credentials_factory=cal_factory,
                calendar_id=cal_id,
            )
            ev, match_method = _find_matching_event(
                events,
                zoom_meeting_id=getattr(r, "zoom_meeting_id", None),
                meeting_date=r.meeting_date,
                meeting_title=r.title,
            )
            date_str = (
                r.meeting_date.strftime("%Y-%m-%d %H:%M")
                if r.meeting_date else "?"
            )
            title = (r.title or "")[:55]
            if ev is None:
                why = "no_events" if not events else "no_event_match"
                print(f"{date_str:<18} {'—  SKIP':<18} {why:<8} {title}")
                skipped.append((r.zoom_id, title, why))
                continue
            sig = _operator_in_event(ev, required)
            if sig:
                print(
                    f"{date_str:<18} {sig:<18} {match_method:<8} {title}"
                )
                kept.append(r.zoom_id)
            else:
                print(
                    f"{date_str:<18} {'—  SKIP':<18} {match_method:<8} {title}"
                )
                skipped.append((r.zoom_id, title, "no_artem_in_event"))

    out = Path(args.out)
    out.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    print("-" * 110)
    print(f"\nTotal: {len(rows)}   Kept (Artem present): {len(kept)}   "
          f"Skipped (no signal): {len(skipped)}")
    print(f"\nFiltered list written to: {out}")
    if skipped:
        print("\nSkipped titles (with reason):")
        for zid, t, why in skipped:
            print(f"  [{why:<18}] {zid}  {t}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
