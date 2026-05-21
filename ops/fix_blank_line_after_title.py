"""FR-CR-05-192h — One-shot fix for short_summary records whose
title line is glued directly to the body / participants line
without the blank line separator the live pipeline always emits.

Bug source: `ops/strip_legacy_todo.py` (initial version) did
`"PLACEHOLDER_TITLE\\n" + stripped_plain.lstrip()`, eating the
`\\n\\n` separator. The script has been patched, but 3 records
(Family Office / Elena Radionova / Ирина 2026-05-20) were already
written with the glued layout.

This fix walks records in the date range, peels the `<a href>`
wrapper off line 1, and if `</a>` is immediately followed by
non-blank content (no `\\n\\n` separator), reinjects the blank
line before re-wrapping. Idempotent — already-correct records skip.

Usage:
    docker exec manager-bot-1 python -m ops.fix_blank_line_after_title \\
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
            if not short.strip() or not short.startswith("<a href="):
                continue
            close_tag_idx = short.find("</a>")
            if close_tag_idx == -1:
                continue
            rest_escaped = short[close_tag_idx + len("</a>"):]
            # Already correct: starts with «\n\n» (blank line).
            if rest_escaped.startswith("\n\n"):
                continue
            # Bug pattern: «</a>» directly followed by non-blank
            # content — single «\n» or no separator at all.
            import html as _html
            rest_plain = _html.unescape(rest_escaped).lstrip("\n")
            if not r.google_doc_url:
                skipped += 1
                continue
            rebuilt = "PLACEHOLDER_TITLE\n\n" + rest_plain
            rebuilt = _force_meeting_title_first_line(
                rebuilt, r.title or "", r.meeting_date,
            )
            rebuilt = _wrap_short_summary_with_doc_link(
                rebuilt.rstrip(), r.google_doc_url,
            )
            print(
                f"  ✓ {r.meeting_date.strftime('%m-%d %H:%M')} "
                f"[{src}] {(r.title or '')[:55]}"
            )
            if not args.dry_run:
                r.short_summary = rebuilt
                session.flush()
            fixed += 1
        if not args.dry_run:
            session.commit()

    print()
    print(
        f"Fixed: {fixed}   "
        f"Skipped (no doc_url): {skipped}   "
        f"{'(dry-run, no commit)' if args.dry_run else 'COMMITTED'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
