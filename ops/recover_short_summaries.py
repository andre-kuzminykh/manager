"""FR-CR-05-192b recovery — regenerate short_summary directly via LLM
for records that have detailed_summary but lost short_summary
(caused by refresh_calendar_attendees clearing them when
_step_short_summary regenerate failed).

Calls SHORT_SUMMARY_SYSTEM with the same meta-block format as the
pipeline, applies the people-canonicalize from FR-CR-05-191, and
writes back.

Usage:
    docker exec manager-bot-1 python -m ops.recover_short_summaries \\
        --start 2026-05-19 --end 2026-05-22 [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from openai import OpenAI

from app.config import get_settings
from app.db import session_scope
from app.fireflies.prompts import SHORT_SUMMARY_SYSTEM
from app.intent.llm_backends import OpenAIBackend
from app.models import MeetingRecording, ZoomRecording


def _truncate(text: str, *, limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rsplit("\n", 1)[0]


def _build_user_prompt(row) -> str:
    cal = row.calendar_attendees or []
    parts: list[str] = []
    if isinstance(cal, list):
        for a in cal:
            if not isinstance(a, dict):
                continue
            # Skip resource calendars (rooms)
            em = a.get("email") or ""
            if "@resource.calendar.google.com" in em:
                continue
            nm = (
                a.get("resolved_name") or a.get("display_name")
                or em or ""
            ).strip()
            if nm:
                parts.append(nm)
    if not parts and getattr(row, "participants", None):
        parts = [p for p in row.participants if p]
    block = "\n".join(f"  - {p}" for p in parts) or "  (нет данных)"
    return (
        f"meeting_title: {row.title or ''}\n"
        f"meeting_date: "
        f"{row.meeting_date.isoformat() if row.meeting_date else ''}\n"
        f"duration_min: "
        f"{row.duration_seconds // 60 if row.duration_seconds else ''}\n"
        f"google_doc_url: {row.google_doc_url or ''}\n\n"
        f"participants:\n{block}\n\n"
        "Подробный отчёт:\n" + (row.detailed_summary or "")
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)

    s = get_settings()
    oc = OpenAI(api_key=s.openai_api_key)
    model = s.fireflies_short_summary_model or s.fireflies_summary_model
    llm = OpenAIBackend(client=oc, model=model)
    print(f"Using model: {model}")

    fixed = 0
    failed = 0
    with session_scope() as session:
        rows: list[tuple[str, object]] = []
        for r in session.query(ZoomRecording).filter(
            ZoomRecording.meeting_date >= start,
            ZoomRecording.meeting_date < end,
        ).all():
            if (r.detailed_summary or "").strip() and not (r.short_summary or "").strip():
                rows.append(("zoom", r))
        for r in session.query(MeetingRecording).filter(
            MeetingRecording.meeting_date >= start,
            MeetingRecording.meeting_date < end,
        ).all():
            if (r.detailed_summary or "").strip() and not (r.short_summary or "").strip():
                rows.append(("fireflies", r))
        rows.sort(key=lambda x: x[1].meeting_date)
        print(f"Records needing recovery: {len(rows)}")
        for i, (src, r) in enumerate(rows, start=1):
            title = (r.title or "")[:55]
            print(f"\n[{i}/{len(rows)}] {r.meeting_date.strftime('%m-%d %H:%M')} [{src}] {title}")
            prompt = _build_user_prompt(r)
            try:
                text = llm.complete_text(
                    system_prompt=SHORT_SUMMARY_SYSTEM,
                    user_prompt=prompt,
                    model=model,
                )
            except Exception as e:  # noqa: BLE001
                print(f"  ERR LLM: {e}")
                failed += 1
                continue
            text = _truncate(text or "", limit=3800)
            if not text:
                print(f"  ERR: LLM returned empty")
                failed += 1
                continue
            print(f"  ✓ regenerated ({len(text)} chars)")
            if not args.dry_run:
                r.short_summary = text
                r.last_error = None
                # Reset sent flag so we can re-send
                r.short_summary_sent = False
                session.flush()
            fixed += 1
        if not args.dry_run:
            session.commit()
    print()
    print(f"Fixed: {fixed}   Failed: {failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
