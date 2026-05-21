#!/usr/bin/env python3
"""FR-CR-05-173 follow-up — filter a date-range of Zoom recordings
to ONLY those where the operator was actually on the call, using
the exact same gate as `ZoomPipeline._operator_actually_present`:

  (a) `calendar_attendees` contains email == ZOOM_REQUIRED_EMAIL, OR
  (b) `participants` (LLM-extracted) contains an «Artem» token, OR
  (c) `transcript_text` head has a speaker tag «Artem ...:».

Operator-pinned 2026-05-21: «что-то много зум встреч, там во всех
есть артем как участник? (1@humanoid.ai)». Filter the bilingual
batch candidates so we re-process only meetings he was on.

Usage:
    docker compose exec -T bot python -m ops.filter_zoom_with_artem \\
        --start 2026-05-19 --end 2026-05-22

Prints a per-row table (zoom_id | date | title | signal) and at the
end writes the filtered list to /tmp/zoom_ids_bilingual_with_artem.txt
(one zoom_id per line, ready for `xargs` into reprocess).
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings
from app.db import session_scope
from app.models import ZoomRecording

_SPEAKER_TAG_RE = re.compile(
    r"(?im)^\s*(?:artem|artyom|артем|артём)"
    r"(?:\s+(?:sokolov|соколов))?\s*:",
)
_OP_TOKENS = ("artem", "артем", "артём", "artyom")


def _presence_signal(row: ZoomRecording, required: str) -> str | None:
    """Return short string describing WHICH signal proves operator
    presence, or None if no signal — keeps the report explainable."""
    for a in (row.calendar_attendees or []):
        if not isinstance(a, dict):
            continue
        email = (a.get("email") or "").strip().lower()
        if email == required:
            return "calendar"
    for p in (row.participants or []):
        if isinstance(p, str) and any(t in p.lower() for t in _OP_TOKENS):
            return "participants"
    head = (row.transcript_text or "")[:8000]
    if _SPEAKER_TAG_RE.search(head):
        return "speaker_tag"
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

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)

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
        skipped: list[tuple[str, str]] = []
        print(f"\n{'date':<20} {'signal':<14} {'host':<26} title")
        print("-" * 110)
        for r in rows:
            sig = _presence_signal(r, required)
            host = (r.host_email or "")[:25]
            date_str = (
                r.meeting_date.strftime("%Y-%m-%d %H:%M")
                if r.meeting_date else "?"
            )
            title = (r.title or "")[:50]
            mark = sig or "—  SKIP"
            print(f"{date_str:<20} {mark:<14} {host:<26} {title}")
            if sig:
                kept.append(r.zoom_id)
            else:
                skipped.append((r.zoom_id, title))

    out = Path(args.out)
    out.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    print("-" * 110)
    print(f"\nTotal: {len(rows)}   Kept (Artem present): {len(kept)}   "
          f"Skipped (no signal): {len(skipped)}")
    print(f"\nFiltered list written to: {out}")
    if skipped:
        print("\nSkipped (operator-absent) titles:")
        for zid, t in skipped:
            print(f"  - {zid}  {t}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
