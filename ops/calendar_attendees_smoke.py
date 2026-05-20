#!/usr/bin/env python3
"""FR-CR-05-169 CLI smoke-test — verify Calendar-driven attendee
resolution on a REAL zoom_id, dry-run (no DB writes).

Usage:
    docker compose exec -T bot python -m ops.calendar_attendees_smoke \\
        --zoom-id "Es0xxBa8RHG4lPl9Z5ZMGQ=="

Steps (each printed in turn):
  1. Load ZoomRecording from DB; show zoom_meeting_id, meeting_date,
     title.
  2. Fetch Calendar events in ±2h window via
     `fetch_calendar_events_via_api`. Show count + first 3 summaries.
  3. Run `resolve_calendar_attendees_for_zoom`. Show match_method,
     event_id, dropped_declined, resolved breakdown
     (team_member / counterparty / unknown).
  4. Render the «Участники» line via `build_meta_block_for_summary`
     against an in-memory mutation of the row (NOT persisted) so
     operator can eyeball the final summary header BEFORE flipping
     the feature on in prod.

Flags:
  --window-minutes N — Calendar fetch window (default 120)
  --persist        — write resolved attendees to row.calendar_attendees
                     and commit. Off by default.
"""
from __future__ import annotations

import argparse
import sys
from typing import Any

from app.config import get_settings
from app.db import session_scope
from app.models import ZoomRecording
from app.services.calendar_attendees import (
    _build_counterparty_email_map,
    reconcile_with_zoom_participants,
    resolve_calendar_attendees_for_zoom,
)
from app.services.calendar_match import fetch_calendar_events_via_api
from app.sync.factories import (
    build_calendar_credentials_factory_with_sa_fallback,
)
from app.zoom.pipeline import build_meta_block_for_summary


def _summary_of(ev: dict[str, Any]) -> str:
    return (ev.get("title") or ev.get("summary") or "(no title)")[:80]


def _fetch_zoom_participants(zoom_id: str) -> list[dict[str, str]]:
    """FR-CR-05-172 — pull `/past_meetings/{uuid}/participants` via
    the existing ZoomClient. Empty list on any failure / missing
    OAuth creds — the smoke degrades gracefully."""
    from app.zoom.client import ZoomClient
    s = get_settings()
    if not (s.zoom_account_id and s.zoom_client_id and s.zoom_client_secret):
        return []
    client = ZoomClient(
        account_id=s.zoom_account_id,
        client_id=s.zoom_client_id,
        client_secret=s.zoom_client_secret,
    )
    try:
        return client.fetch_meeting_participants(zoom_id) or []
    except Exception as e:  # noqa: BLE001
        print(f"  zoom fetch_meeting_participants failed: {e}")
        return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zoom-id", required=True)
    ap.add_argument("--window-minutes", type=int, default=120)
    ap.add_argument(
        "--persist", action="store_true",
        help="Commit resolved attendees to the DB row (default off).",
    )
    args = ap.parse_args()

    settings = get_settings()

    print("=" * 70)
    print(f"Zoom ID: {args.zoom_id}")
    print(f"Calendar IDs: {settings.google_calendar_id}")
    print(f"Window: ±{args.window_minutes} min")
    print(f"Persist: {args.persist}")
    print("=" * 70)

    with session_scope() as session:
        row = (
            session.query(ZoomRecording)
            .filter(ZoomRecording.zoom_id == args.zoom_id)
            .first()
        )
        if row is None:
            print(f"\nERROR: no ZoomRecording with zoom_id={args.zoom_id}",
                  file=sys.stderr)
            return 2

        print("\n[1/4] ZoomRecording row:")
        print(f"  title:            {row.title!r}")
        print(f"  zoom_meeting_id:  {row.zoom_meeting_id!r}")
        print(f"  meeting_date:     {row.meeting_date}")
        print(f"  duration_seconds: {row.duration_seconds}")
        print(f"  participants:     {row.participants}")
        # Don't assume the column already exists in prod DB.
        existing_cal = getattr(row, "calendar_attendees", None)
        print(f"  calendar_attendees (current): {existing_cal}")

        if row.meeting_date is None:
            print("\nERROR: row.meeting_date is NULL — can't query Calendar",
                  file=sys.stderr)
            return 3

        print("\n[2/4] Fetching Calendar events in ±"
              f"{args.window_minutes} min…")
        try:
            cal_factory = build_calendar_credentials_factory_with_sa_fallback(
                settings,
            )
        except Exception as e:  # noqa: BLE001
            print(f"ERROR: calendar factory build failed: {e}",
                  file=sys.stderr)
            return 4
        if cal_factory is None:
            print("ERROR: calendar factory unavailable (no OAuth + no SA fallback)",
                  file=sys.stderr)
            return 4

        try:
            events = fetch_calendar_events_via_api(
                meeting_dt=row.meeting_date,
                window_minutes=args.window_minutes,
                credentials_factory=cal_factory,
                calendar_id=settings.google_calendar_id,
            ) or []
        except Exception as e:  # noqa: BLE001
            print(f"ERROR: fetch_calendar_events_via_api failed: {e}",
                  file=sys.stderr)
            return 5
        print(f"  → {len(events)} event(s) in window")
        for ev in events[:10]:
            cal_id = ev.get("_calendar_id") or "?"
            desc = (ev.get("description") or "").replace("\n", " ")
            has_zid = (
                row.zoom_meeting_id and row.zoom_meeting_id in (
                    ev.get("description") or ""
                )
            )
            tag = "✓ZID" if has_zid else "    "
            print(f"    [{tag}] [{cal_id}] {_summary_of(ev)!r:50} | "
                  f"attendees={len(ev.get('attendees') or []):2d} | "
                  f"start={ev.get('start')} | "
                  f"desc[:80]={desc[:80]!r}")

        print("\n[3/4] Resolving attendees…")
        resolved = resolve_calendar_attendees_for_zoom(
            row, session, calendar_events=events,
        )
        if not resolved:
            print("  → NO MATCH (no event matches by URL or fuzzy)")
            print("  → Pipeline will fall back to LLM-extracted participants.")
            return 0

        print(f"  match_method:     {resolved['match_method']}")
        print(f"  event_id:         {resolved['event_id']}")
        print(f"  total attendees:  {len(resolved['attendees'])}")
        print(f"  resolved:         {resolved['resolved_count']}")
        print(f"  unknown:          {resolved['unknown_count']}")
        print(f"  dropped declined: {resolved['dropped_declined']}")
        print("\n  Attendees:")
        for a in resolved["attendees"]:
            tag = a["source"].upper().ljust(13)
            print(f"    [{tag}] {a['resolved_name']!r}  "
                  f"(email={a['email']}, status={a['response_status']})")

        print("\n[3.5/4] FR-CR-05-172 — cross-reference with actual "
              "Zoom participants…")
        zoom_participants = _fetch_zoom_participants(args.zoom_id)
        if not zoom_participants:
            print("  → Zoom API returned 0 participants (or auth failed)."
                  " Skipping reconcile, keeping Calendar list as-is.")
            final_attendees = resolved["attendees"]
        else:
            print(f"  → {len(zoom_participants)} Zoom participants:")
            for p in zoom_participants:
                print(f"    - {p.get('user_name')!r:30s} "
                      f"{p.get('user_email')!r}")
            openai_client = None
            api_key = (s.openai_api_key or "").strip()
            if api_key:
                from openai import OpenAI
                openai_client = OpenAI(api_key=api_key)
            from app.agenda.service import _build_email_to_name_map
            email_to_team = _build_email_to_name_map(session)
            email_to_cp = _build_counterparty_email_map(session)
            def _resolver(email):
                e = (email or "").strip().lower()
                if e in email_to_team:
                    return {"resolved_name": email_to_team[e],
                            "source": "team_member"}
                if e in email_to_cp:
                    return {"resolved_name": email_to_cp[e],
                            "source": "counterparty"}
                return None
            rec = reconcile_with_zoom_participants(
                calendar_attendees=resolved["attendees"],
                zoom_participants=zoom_participants,
                openai_client=openai_client,
                email_resolver=_resolver,
            )
            print(f"  match method counts: {rec['method_breakdown']}")
            print(f"  llm_used: {rec['llm_used']}")
            print(f"  joined: {len(rec['attendees'])}")
            for a in rec["attendees"]:
                tag = a["zoom_join_method"].upper().ljust(10)
                print(f"    [{tag}] {a['resolved_name']!r:30s} "
                      f"(src={a['source']}, email={a['email']!r})")
            if rec["unmatched_calendar"]:
                print(f"  invited but didn't join: "
                      f"{[a['resolved_name'] for a in rec['unmatched_calendar']]}")
            final_attendees = rec["attendees"]

        print("\n[4/4] Preview of «Участники» line in summary header:")
        # Temporarily set the field for rendering — do NOT commit
        # unless --persist is on.
        if hasattr(row, "calendar_attendees"):
            row.calendar_attendees = final_attendees
            block = build_meta_block_for_summary(row)
            print("  --- meta block ---")
            for line in block.splitlines():
                print(f"  {line}")
            print("  ------------------")
            if not args.persist:
                # Rollback the in-memory mutation so no accidental
                # commit happens on session exit.
                session.expire(row)
                print("\n  (dry-run — DB NOT updated; pass --persist to commit)")
            else:
                session.commit()
                print("\n  ✓ row.calendar_attendees committed to DB")
        else:
            print("  WARNING: `calendar_attendees` field not on model. "
                  "Apply migration 0032 first.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
