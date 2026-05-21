"""Operator-pinned 2026-05-21 — inventory всех его встреч 19-21 мая.

Источники:
  - Zoom REST  (`accounts/me/recordings`) — внутренние
  - Fireflies GraphQL (`transcripts`)     — внешние
  - Google Calendar API                   — для подтверждения
    «Артем среди attendees / organizer / creator»

Для каждой записи:
  - source         : zoom | fireflies
  - date           : start UTC
  - title          : meeting topic / title
  - in_db          : Y/N — уже в нашей БД?
  - transcribed    : Y/N — есть transcript_text в БД
  - bilingual_done : Y/N — для Zoom: транскрипт прошёл bilingual
  - cal_event      : id найденного календарного события (или —)
  - artem_role     : organizer / creator / attendee / —
  - keep           : ✓ / ✗ — попадает в bilingual / re-summary батч

Не делает никаких изменений в БД. Только читает и отчитывается.

Usage:
    docker compose exec -T bot python -m ops.meeting_inventory \\
        --start 2026-05-19 --end 2026-05-22 \\
        --required-email 1@thehumanoid.ai
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import get_settings
from app.db import session_scope
from app.fireflies.client import FirefliesClient
from app.models import MeetingRecording, ZoomRecording
from app.services.calendar_attendees import _find_matching_event
from app.services.calendar_match import fetch_calendar_events_via_api
from app.sync.factories import (
    build_calendar_credentials_factory_with_sa_fallback,
)
from app.zoom.client import ZoomClient


def _artem_role(ev: dict[str, Any], required: str) -> str | None:
    if not ev:
        return None
    org = (ev.get("organizer") or {}).get("email", "").strip().lower()
    if org == required:
        return "organizer"
    cre = (ev.get("creator") or {}).get("email", "").strip().lower()
    if cre == required:
        return "creator"
    for a in (ev.get("attendees") or []):
        if not isinstance(a, dict):
            continue
        if (a.get("email") or "").strip().lower() != required:
            continue
        if (a.get("responseStatus") or "").strip().lower() == "declined":
            return "declined"
        return "attendee"
    return None


def _format_row(
    *, source: str, dt: datetime | None, title: str,
    in_db: bool, txt_chars: int,
    cal_method: str, role: str | None,
) -> str:
    date_str = dt.strftime("%m-%d %H:%M") if dt else "?"
    in_db_s = "✓" if in_db else "—"
    txt_s = f"{txt_chars//1000}k" if txt_chars else "—"
    role_s = role or "—"
    keep = "✓" if role in ("organizer", "creator", "attendee") else "✗"
    return (
        f"{source:<8} {date_str:<13} {in_db_s:<4} {txt_s:<5} "
        f"{cal_method:<6} {role_s:<10} {keep:<3} {title[:60]}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--required-email", default=None)
    ap.add_argument(
        "--fireflies-limit", type=int, default=80,
        help="How many recent Fireflies transcripts to scan "
        "(API doesn't support date filter; we scan from latest "
        "and stop once we go past --start).",
    )
    args = ap.parse_args()

    s = get_settings()
    required = (
        args.required_email or s.zoom_required_email or ""
    ).strip().lower()
    if not required:
        print("ERROR: pass --required-email", file=sys.stderr)
        return 2

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)

    cal_factory = build_calendar_credentials_factory_with_sa_fallback(s)
    cal_id = s.google_calendar_id or "primary"
    if cal_factory is None:
        print("ERROR: no Calendar credentials.", file=sys.stderr)
        return 3

    print(f"\n# Inventory {args.start} .. {args.end} for {required}\n")

    # ---- Zoom -------------------------------------------------
    print("==> Zoom REST: list_recordings")
    zm = ZoomClient(
        account_id=s.zoom_account_id,
        client_id=s.zoom_client_id,
        client_secret=s.zoom_client_secret,
        api_base=s.zoom_api_base,
        oauth_url=s.zoom_oauth_url,
    )
    zoom_metas = zm.list_recordings(
        limit=200, page_size=100,
        from_date=args.start, to_date=args.end,
        required_email=None,  # фильтруем сами в Python
        strict_host=False,
    )
    print(f"   Zoom вернул {len(zoom_metas)} записей")

    # ---- Fireflies --------------------------------------------
    print("==> Fireflies GraphQL: list_transcripts")
    ff = FirefliesClient(
        token=s.fireflies_api_token,
        endpoint=s.fireflies_api_url,
    )
    ff_all = ff.list_transcripts(limit=args.fireflies_limit, skip=0)
    ff_in_range = [
        t for t in ff_all
        if t.meeting_date and start <= t.meeting_date < end
    ]
    print(
        f"   Fireflies вернул {len(ff_all)} последних "
        f"({len(ff_in_range)} в окне)"
    )

    # ---- Header -----------------------------------------------
    print(
        "\n"
        f"{'source':<8} {'date':<13} {'db':<4} {'txt':<5} "
        f"{'cal':<6} {'role':<10} {'k':<3} title"
    )
    print("-" * 130)

    with session_scope() as session:
        # ---- Zoom rows --------------------------------------
        kept_zoom: list[str] = []
        for m in zoom_metas:
            dt = m.meeting_date
            if dt is None or not (start <= dt < end):
                continue
            row = session.query(ZoomRecording).filter(
                ZoomRecording.zoom_id == m.id
            ).first()
            in_db = row is not None
            txt_chars = len(row.transcript_text or "") if row else 0
            events = fetch_calendar_events_via_api(
                dt, window_minutes=30,
                credentials_factory=cal_factory,
                calendar_id=cal_id,
            )
            ev, match_method = _find_matching_event(
                events,
                zoom_meeting_id=m.meeting_id,
                meeting_date=dt,
                meeting_title=m.title,
            )
            role = _artem_role(ev or {}, required) if ev else None
            print(_format_row(
                source="zoom", dt=dt, title=m.title or "",
                in_db=in_db, txt_chars=txt_chars,
                cal_method=match_method or "—",
                role=role,
            ))
            if role in ("organizer", "creator", "attendee"):
                kept_zoom.append(m.id)

        # ---- Fireflies rows ----------------------------------
        kept_ff: list[str] = []
        for t in ff_in_range:
            dt = t.meeting_date
            row = session.query(MeetingRecording).filter(
                MeetingRecording.fireflies_id == t.id
            ).first()
            in_db = row is not None
            txt_chars = len(row.transcript_text or "") if row else 0
            events = fetch_calendar_events_via_api(
                dt, window_minutes=30,
                credentials_factory=cal_factory,
                calendar_id=cal_id,
            )
            ev, match_method = _find_matching_event(
                events,
                zoom_meeting_id=None,  # Fireflies не имеет zoom_meeting_id
                meeting_date=dt,
                meeting_title=t.title,
            )
            role = _artem_role(ev or {}, required) if ev else None
            print(_format_row(
                source="fireflies", dt=dt, title=t.title or "",
                in_db=in_db, txt_chars=txt_chars,
                cal_method=match_method or "—",
                role=role,
            ))
            if role in ("organizer", "creator", "attendee"):
                kept_ff.append(t.id)

    print("-" * 130)
    print(
        f"\nZoom kept: {len(kept_zoom)}    "
        f"Fireflies kept: {len(kept_ff)}"
    )
    print(
        "\nKept = Artem is organizer/creator/attendee (non-declined) of "
        "the matched Calendar event."
    )
    print(
        "\nNB: bilingual column for Fireflies is 'n/a' — bilingual "
        "restoration is Zoom-only (внешние встречи у Fireflies "
        "уже корректные)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
