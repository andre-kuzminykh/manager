"""FR-CR-05-191 retro — canonicalize names in existing 19-21 May
short_summary + detailed_summary using TeamMember + Counterparty
directories.

Reads each Zoom + Fireflies recording in date range, runs the
new ``canonicalize_summary_text`` service over both summaries,
writes back if anything changed.

Usage:
    docker exec manager-bot-1 python -m ops.canonicalize_existing_summaries \\
        --start 2026-05-19 --end 2026-05-22 [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from app.config import get_settings
from app.db import session_scope
from app.intent.llm_backends import OpenAIBackend
from app.models import MeetingRecording, ZoomRecording
from app.services.summary_canonicalize import canonicalize_summary_text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--skip-detailed", action="store_true",
        help="Only canonicalize short_summary (faster, less LLM cost).",
    )
    ap.add_argument(
        "--skip-zoom", action="store_true",
    )
    ap.add_argument(
        "--skip-fireflies", action="store_true",
    )
    args = ap.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)

    s = get_settings()
    from openai import OpenAI
    oc = OpenAI(api_key=s.openai_api_key)
    llm = OpenAIBackend(client=oc, model=s.fireflies_tasks_model)
    model = s.fireflies_tasks_model

    rewrites_total = 0
    rows_touched = 0
    with session_scope() as session:
        if not args.skip_zoom:
            for r in session.query(ZoomRecording).filter(
                ZoomRecording.meeting_date >= start,
                ZoomRecording.meeting_date < end,
            ).order_by(ZoomRecording.meeting_date).all():
                if not (r.short_summary or "").strip():
                    continue
                print(f"  ZOOM {r.meeting_date.strftime('%m-%d %H:%M')} | "
                      f"{(r.title or '')[:55]}")
                changed = False
                if not args.skip_detailed and (r.detailed_summary or "").strip():
                    new_text, applied = canonicalize_summary_text(
                        r.detailed_summary,
                        session=session, llm_backend=llm, model=model,
                        trace_source="zoom_detailed",
                        trace_recording_id=r.zoom_id,
                    )
                    if applied:
                        print(f"      detailed rewrites: {applied}")
                        if not args.dry_run:
                            r.detailed_summary = new_text
                        rewrites_total += len(applied)
                        changed = True
                new_text, applied = canonicalize_summary_text(
                    r.short_summary,
                    session=session, llm_backend=llm, model=model,
                    trace_source="zoom_short",
                    trace_recording_id=r.zoom_id,
                )
                if applied:
                    print(f"      short rewrites: {applied}")
                    if not args.dry_run:
                        r.short_summary = new_text
                    rewrites_total += len(applied)
                    changed = True
                if changed:
                    rows_touched += 1
        if not args.skip_fireflies:
            for r in session.query(MeetingRecording).filter(
                MeetingRecording.meeting_date >= start,
                MeetingRecording.meeting_date < end,
            ).order_by(MeetingRecording.meeting_date).all():
                if not (r.short_summary or "").strip():
                    continue
                print(f"  FF   {r.meeting_date.strftime('%m-%d %H:%M')} | "
                      f"{(r.title or '')[:55]}")
                changed = False
                if not args.skip_detailed and (r.detailed_summary or "").strip():
                    new_text, applied = canonicalize_summary_text(
                        r.detailed_summary,
                        session=session, llm_backend=llm, model=model,
                        trace_source="ff_detailed",
                        trace_recording_id=r.fireflies_id,
                    )
                    if applied:
                        print(f"      detailed rewrites: {applied}")
                        if not args.dry_run:
                            r.detailed_summary = new_text
                        rewrites_total += len(applied)
                        changed = True
                new_text, applied = canonicalize_summary_text(
                    r.short_summary,
                    session=session, llm_backend=llm, model=model,
                    trace_source="ff_short",
                    trace_recording_id=r.fireflies_id,
                )
                if applied:
                    print(f"      short rewrites: {applied}")
                    if not args.dry_run:
                        r.short_summary = new_text
                    rewrites_total += len(applied)
                    changed = True
                if changed:
                    rows_touched += 1
        if not args.dry_run:
            session.commit()
    print()
    print(f"Rows touched: {rows_touched}")
    print(f"Total rewrites applied: {rewrites_total}")
    if args.dry_run:
        print("--dry-run — no commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
