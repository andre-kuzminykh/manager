"""Re-run ONLY the calendar-title match for one Fireflies meeting.

Use when a meeting was processed while CALENDAR_MATCH_ENABLED was off (or the
Fireflies title-push schema was broken), so it kept the auto-stamp title
(«Jun 02, 01:03 PM») instead of the canonical «DD/MM - <calendar event>».

This runs SOLELY `_step_match_calendar_title` — it does NOT re-extract tasks,
re-summarise, or re-post, so nothing is duplicated. It re-matches the meeting
against Google Calendar, rewrites `row.title` to «DD/MM - …» and pushes that
title back to Fireflies (same single mechanism as the live pipeline).

Requires CALENDAR_MATCH_ENABLED=true and the calendar OAuth creds in the DB.

    docker exec manager-zoom-ff-1 python -m ops.fireflies_rematch_title \\
        --fireflies-id 01KT43FV9QYA2SEK7S94SZ82BW
"""
from __future__ import annotations

import argparse
import sys

from openai import OpenAI

from app.config import get_settings
from app.db import session_scope
from app.fireflies.client import FirefliesClient
from app.fireflies.pipeline import FirefliesPipeline
from app.intent.llm_backends import OpenAIBackend
from app.logging_setup import get_logger, setup_logging
from app.models import MeetingRecording
from app.sync.factories import build_calendar_credentials_factory_with_sa_fallback

log = get_logger(__name__)


def main() -> int:
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--fireflies-id", required=True)
    args = ap.parse_args()

    s = get_settings()
    if not getattr(s, "calendar_match_enabled", False):
        print("ERROR: CALENDAR_MATCH_ENABLED is off — enable it first",
              file=sys.stderr)
        return 2

    try:
        cal = build_calendar_credentials_factory_with_sa_fallback(s)
    except Exception:  # noqa: BLE001
        cal = None
    pipeline = FirefliesPipeline(
        settings=s,
        client=FirefliesClient(token=s.fireflies_api_token,
                               endpoint=s.fireflies_api_url),
        llm_backend=OpenAIBackend(OpenAI(api_key=s.openai_api_key), s.openai_model),
        docs_factory=None, sender=None, calendar_factory=cal,
    )

    with session_scope() as session:
        row = (
            session.query(MeetingRecording)
            .filter(MeetingRecording.fireflies_id == args.fireflies_id)
            .one_or_none()
        )
        if row is None:
            print(f"ERROR: no meeting_recordings row for fireflies_id="
                  f"{args.fireflies_id}", file=sys.stderr)
            return 2
        if not (row.detailed_summary or "").strip():
            print("ERROR: row has no detailed_summary — calendar match needs it",
                  file=sys.stderr)
            return 2
        old_title = row.title
        # ONLY the title step — no task extraction, no summary, no re-post.
        updated = pipeline._step_match_calendar_title(session, row)
        new_title = row.title
        session.commit()

    print(f"old: {old_title!r}")
    print(f"new: {new_title!r}")
    print(f"updated={'yes' if updated else 'no (no calendar match → unchanged)'}")
    log.info("fireflies_rematch_title_done", fireflies_id=args.fireflies_id,
             old=old_title, new=new_title, updated=bool(updated))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
