"""FR-CR-05-178 follow-up — для конкретного `zoom_id` показать
живой резолв участников **без** запуска пайплайна (Whisper и
LLM-вызовы не нужны). Источник:

  1. ZoomRecording.meeting_date + .title (из БД)
  2. Google Calendar API: события в окне ±120 мин
  3. `_find_matching_event` → URL/fuzzy match
  4. `_resolve_attendee` → TeamMember / Employee / Counterparty lookup
  5. (опционально) Zoom REST `/past_meetings/{uuid}/participants`
     для reconcile с реально подключившимися

Read-only. БД не пишет.

Usage:
    docker compose exec -T bot python -m ops.show_attendees_live \\
        --zoom-id "..."
"""
from __future__ import annotations

import argparse
import sys

from app.config import get_settings
from app.db import session_scope
from app.models import ZoomRecording
from app.services.calendar_attendees import (
    _find_matching_event,
    _resolve_attendee,
    _build_counterparty_email_map,
    _build_email_to_name_map,
    reconcile_with_zoom_participants,
)
from app.services.calendar_match import fetch_calendar_events_via_api
from app.sync.factories import (
    build_calendar_credentials_factory_with_sa_fallback,
)
from app.zoom.client import ZoomClient


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zoom-id", required=True)
    args = ap.parse_args()

    s = get_settings()
    with session_scope() as session:
        row = session.query(ZoomRecording).filter(
            ZoomRecording.zoom_id == args.zoom_id
        ).first()
        if row is None:
            print(f"ERROR: no row for zoom_id={args.zoom_id!r}",
                  file=sys.stderr)
            return 2

        print("\n" + "=" * 78)
        print(f"zoom_id:       {row.zoom_id}")
        print(f"title:         {row.title!r}")
        print(f"meeting_date:  {row.meeting_date}")
        print(f"zoom_meeting:  {row.zoom_meeting_id}")
        print(f"host_email:    {row.host_email!r}")
        print()

        cal_factory = build_calendar_credentials_factory_with_sa_fallback(s)
        if cal_factory is None:
            print("ERROR: no Calendar credentials.", file=sys.stderr)
            return 3

        print("[1/4] Fetching Calendar events ±120 мин...")
        events = fetch_calendar_events_via_api(
            meeting_dt=row.meeting_date,
            window_minutes=120,
            credentials_factory=cal_factory,
            calendar_id=s.google_calendar_id,
        ) or []
        print(f"      → {len(events)} events in window")
        for ev in events:
            ev_start = (
                ev.get("start") if isinstance(ev.get("start"), str)
                else (ev.get("start") or {}).get("dateTime", "?")
            )
            print(
                f"        - {ev_start:<32} «{(ev.get('title') or '')[:50]}»  "
                f"id={ev.get('id', '?')[:30]}"
            )

        print("\n[2/4] Matching to recording...")
        ev, match_method = _find_matching_event(
            events,
            zoom_meeting_id=row.zoom_meeting_id,
            meeting_date=row.meeting_date,
            meeting_title=row.title,
        )
        if ev is None:
            print("      → NO MATCH (no Calendar event matched URL or fuzzy)")
            return 0
        print(
            f"      → matched ({match_method}): «{ev.get('title')!r}» "
            f"id={ev.get('id')}"
        )
        org = (ev.get("organizer") or {}).get("email", "")
        cre = (ev.get("creator") or {}).get("email", "")
        print(f"      organizer.email: {org}")
        print(f"      creator.email:   {cre}")

        print("\n[3/4] Resolving attendees via People/Counterparty tables...")
        raw_attendees = ev.get("attendees") or []
        print(f"      raw attendees count: {len(raw_attendees)}")
        email_to_team = _build_email_to_name_map(session)
        email_to_cp = _build_counterparty_email_map(session)
        resolved: list[dict] = []
        for item in raw_attendees:
            if not isinstance(item, dict):
                continue
            if (item.get("responseStatus") or "").lower() == "declined":
                print(f"        - DECLINED: {item.get('email')}")
                continue
            r = _resolve_attendee(
                item,
                email_to_team_name=email_to_team,
                email_to_counterparty_name=email_to_cp,
            )
            resolved.append(r)
            print(
                f"        • {r['resolved_name']:<35} "
                f"<{r['email']:<35}> "
                f"src={r['source']:<14} status={r['response_status']}"
            )

        print("\n[4/4] Reconcile with Zoom participants (who joined)...")
        zm = ZoomClient(
            account_id=s.zoom_account_id,
            client_id=s.zoom_client_id,
            client_secret=s.zoom_client_secret,
            api_base=s.zoom_api_base,
            oauth_url=s.zoom_oauth_url,
        )
        try:
            zoom_parts = zm.fetch_meeting_participants(row.zoom_id) or []
        except Exception as e:  # noqa: BLE001
            zoom_parts = []
            print(f"      Zoom API err: {e}")
        print(f"      Zoom join list count: {len(zoom_parts)}")
        for p in zoom_parts:
            print(
                f"        • {p.get('user_name', '?'):<35} "
                f"<{p.get('user_email', '')}>"
            )

        if not zoom_parts:
            print("\nFinal attendees = Calendar invited (no Zoom data to reconcile)")
            final = resolved
        else:
            # FR-CR-05-172 — LLM fallback for cyrillic↔latin name
            # mismatches («Дмитрий Седов» vs «Dmitry Sedov») that the
            # pure-fuzzy step misses. Without an OpenAI client those
            # entries get dropped as `zoom_only` with raw Zoom names.
            openai_client = None
            if s.openai_api_key:
                try:
                    from openai import OpenAI
                    openai_client = OpenAI(api_key=s.openai_api_key)
                except Exception:  # noqa: BLE001
                    openai_client = None
            reconcile = reconcile_with_zoom_participants(
                calendar_attendees=resolved,
                zoom_participants=zoom_parts,
                openai_client=openai_client,
                llm_model="gpt-4o-mini",
            )
            final = reconcile.get("attendees") or []
            print(
                f"\nReconcile: joined={len(final)} "
                f"breakdown={reconcile.get('method_breakdown')}"
            )

        print("\n>>> FINAL «Участники: …» список:")
        names = [
            (a.get("resolved_name") or a.get("display_name")
             or a.get("email") or "").strip()
            for a in final
            if isinstance(a, dict)
        ]
        names = [n for n in names if n]
        if names:
            print(f"   {', '.join(names)}")
        else:
            print("   (empty)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
