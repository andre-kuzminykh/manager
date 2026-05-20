#!/usr/bin/env python3
"""FR-CR-05-172 + FR-CR-05-173 fast verifier — no LLM, no re-process.

For each given zoom_id:
  1. Fetch the row from DB.
  2. Pull Calendar event + resolved attendees (FR-CR-05-169 path,
     already verified in prod).
  3. Pull Zoom participants via the NEW scope (FR-CR-05-172).
  4. Run `reconcile_with_zoom_participants` — show match breakdown.
  5. Run `_operator_actually_present` — show the gate verdict
     (FR-CR-05-173).
  6. Print: «would this row be POSTED to Slack or SUPPRESSED?»

Usage:
    docker compose exec -T bot python -m ops.verify_zoom_gate \\
        --zoom-id Es0xxBa8RHG4lPl9Z5ZMGQ== \\
        --zoom-id 8ULjRNgQR+Ssc+o3KS4f+Q==
"""
from __future__ import annotations

import argparse
import sys

from app.agenda.service import _build_email_to_name_map
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
from app.zoom.client import ZoomClient
from app.zoom.pipeline import ZoomPipeline


def check_one(zoom_id: str, *, settings, cal_factory, zoom_client,
              openai_client) -> None:
    print("=" * 78)
    print(f"zoom_id: {zoom_id}")
    print("=" * 78)
    with session_scope() as session:
        row = session.query(ZoomRecording).filter(
            ZoomRecording.zoom_id == zoom_id
        ).first()
        if row is None:
            print("  ❌ NOT FOUND in DB.")
            return
        print(f"  title:        {row.title!r}")
        print(f"  meeting_date: {row.meeting_date}")
        print(f"  host_email:   {row.host_email!r}")
        print(f"  duration:     {row.duration_seconds}s")
        print(f"  participants (LLM-extracted): {row.participants}")

        # 1. Calendar resolve.
        cal_attendees = list(row.calendar_attendees or [])
        if not cal_attendees and row.meeting_date is not None:
            print("\n  [calendar] live fetch + resolve…")
            try:
                events = fetch_calendar_events_via_api(
                    meeting_dt=row.meeting_date,
                    window_minutes=120,
                    credentials_factory=cal_factory,
                    calendar_id=settings.google_calendar_id,
                ) or []
            except Exception as e:  # noqa: BLE001
                print(f"    calendar fetch failed: {e}")
                events = []
            resolved = resolve_calendar_attendees_for_zoom(
                row, session, calendar_events=events,
            )
            cal_attendees = (resolved or {}).get("attendees") or []
            print(f"    match_method: {(resolved or {}).get('match_method')!r}")
        print(f"  calendar_attendees ({len(cal_attendees)}):")
        for a in cal_attendees:
            print(f"    - {a.get('resolved_name')!r:30s} email={a.get('email')!r}")

        # 2. Zoom participants (FR-CR-05-172).
        print("\n  [zoom] fetch_meeting_participants…")
        zoom_parts = zoom_client.fetch_meeting_participants(zoom_id)
        if not zoom_parts:
            print("    ❌ Zoom API returned 0 — scope still missing or "
                  "no participants recorded.")
        else:
            print(f"    ✓ {len(zoom_parts)} participants:")
            for p in zoom_parts:
                print(f"      - {p.get('user_name')!r:35s} {p.get('user_email')!r}")

        # 3. Reconcile.
        email_to_team_name = _build_email_to_name_map(session)
        email_to_cp_name = _build_counterparty_email_map(session)
        def _resolver(email):
            e = (email or "").strip().lower()
            if e in email_to_team_name:
                return {"resolved_name": email_to_team_name[e],
                        "source": "team_member"}
            if e in email_to_cp_name:
                return {"resolved_name": email_to_cp_name[e],
                        "source": "counterparty"}
            return None
        rec = reconcile_with_zoom_participants(
            calendar_attendees=cal_attendees,
            zoom_participants=zoom_parts,
            openai_client=openai_client,
            email_resolver=_resolver,
        )
        merged = rec["attendees"]
        print(f"\n  [reconcile] {len(merged)} final attendees, "
              f"breakdown={rec['method_breakdown']}, "
              f"llm_used={rec['llm_used']}")
        for a in merged:
            tag = (a.get("zoom_join_method") or "?").upper().ljust(10)
            print(f"    [{tag}] {a.get('resolved_name')!r:30s} "
                  f"(src={a.get('source')})")
        if rec.get("unmatched_calendar"):
            print(f"  [invited but did NOT join Zoom]:")
            for a in rec["unmatched_calendar"]:
                print(f"    - {a.get('resolved_name')}")

        # 4. Operator-presence gate (FR-CR-05-173).
        # Temporarily set the merged list on the row so the gate
        # sees the freshest data.
        row_proxy = type("RowProxy", (), {})()
        row_proxy.calendar_attendees = merged
        row_proxy.participants = row.participants or []
        row_proxy.transcript_text = row.transcript_text or ""
        pipe_proxy = type("PipeProxy", (), {})()
        pipe_proxy._settings = settings
        present = ZoomPipeline._operator_actually_present(
            pipe_proxy, row_proxy,
        )
        print(f"\n  [presence gate] operator_actually_present = {present}")
        if present:
            print(f"  → ✅ Slack short_summary WOULD be POSTED.")
        else:
            print(f"  → 🚫 Slack short_summary would be SUPPRESSED "
                  f"(FR-CR-05-173 — operator not on call).")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--zoom-id", action="append", required=True,
        help="May be repeated to check multiple recordings.",
    )
    args = ap.parse_args()

    s = get_settings()
    cal_factory = build_calendar_credentials_factory_with_sa_fallback(s)
    if cal_factory is None:
        print("ERROR: calendar factory unavailable.", file=sys.stderr)
        return 2
    zoom_client = ZoomClient(
        account_id=s.zoom_account_id,
        client_id=s.zoom_client_id,
        client_secret=s.zoom_client_secret,
    )
    openai_client = None
    if s.openai_api_key:
        try:
            from openai import OpenAI
            openai_client = OpenAI(api_key=s.openai_api_key)
        except Exception:  # noqa: BLE001
            pass
    for zid in args.zoom_id:
        check_one(
            zid,
            settings=s, cal_factory=cal_factory,
            zoom_client=zoom_client, openai_client=openai_client,
        )
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
