"""FR-CR-05-192d fixup — wrap recovered short_summary first lines
with the `<a href>` title hyperlink the live pipeline applies after
`_step_short_summary`.

`ops/recover_short_summaries.py` writes the raw LLM output to
``short_summary``. The live pipeline post-processes it via two helpers
in ``app/fireflies/pipeline.py``:

  1. ``_force_meeting_title_first_line`` — replaces line 1 with
     ``"DD/MM - <raw row.title>"`` (FR-CR-05-156).
  2. ``_wrap_short_summary_with_doc_link`` — HTML-escapes the body,
     wraps line 1 in ``<a href="<doc_url>">…</a>`` (FR-CR-05-127).

This script applies BOTH to every record in the date range whose
``short_summary`` exists, has a ``google_doc_url``, and whose first
line does NOT already start with ``<a href``.

Usage:
    docker exec manager-bot-1 python -m ops.wrap_recovered_summaries \\
        --start 2026-05-19 --end 2026-05-22 [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from app.db import session_scope
from app.fireflies.pipeline import (
    _force_meeting_title_first_line,
    _wrap_short_summary_with_doc_link,
)
from app.models import MeetingRecording, ZoomRecording


def _needs_wrap(text: str) -> bool:
    if not text:
        return False
    first = text.split("\n", 1)[0]
    return "<a href=" not in first


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)

    fixed = 0
    skipped = 0
    with session_scope() as session:
        rows: list[tuple[str, object]] = []
        for r in session.query(ZoomRecording).filter(
            ZoomRecording.meeting_date >= start,
            ZoomRecording.meeting_date < end,
        ).all():
            rows.append(("zoom", r))
        for r in session.query(MeetingRecording).filter(
            MeetingRecording.meeting_date >= start,
            MeetingRecording.meeting_date < end,
        ).all():
            rows.append(("fireflies", r))
        rows.sort(key=lambda x: x[1].meeting_date)

        for src, r in rows:
            short = r.short_summary or ""
            if not short.strip():
                continue
            if not (r.google_doc_url or "").strip():
                skipped += 1
                continue
            if not _needs_wrap(short):
                continue
            body = _force_meeting_title_first_line(
                short, r.title or "", r.meeting_date,
            )
            body = _wrap_short_summary_with_doc_link(
                body.rstrip(), r.google_doc_url,
            )
            print(
                f"  ✓ {r.meeting_date.strftime('%m-%d %H:%M')} "
                f"[{src}] {(r.title or '')[:55]}"
            )
            if not args.dry_run:
                r.short_summary = body
                session.flush()
            fixed += 1
        if not args.dry_run:
            session.commit()

    print()
    print(f"Wrapped: {fixed}   Skipped (no doc_url): {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
