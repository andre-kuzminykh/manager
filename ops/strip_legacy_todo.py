"""FR-CR-05-192g fixup — strip leftover «To-Do:» / «Следующие
шаги:» / «Action items:» blocks from `short_summary`.

`ops/recover_short_summaries.py` regenerated short_summary via the
LLM, but the LLM consistently disobeys the SHORT_SUMMARY_SYSTEM
instruction «MUST NOT emit a To-Do section» — so the recovered
text carries an embedded To-Do block. The live send pipeline
already builds the deterministic To-Do from the Task table and
posts it as the Slack thread reply (FR-CR-05-178), so leaving the
LLM-emitted block in the parent body would result in DUPLICATE
task lists in Slack.

Idempotent: applies the same `_strip_llm_todo_block` helper the
pipeline calls right after the short-summary LLM call. Records
that already have no leftover block are skipped.

Usage:
    docker exec manager-bot-1 python -m ops.strip_legacy_todo \\
        --start 2026-05-19 --end 2026-05-22 [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from app.db import session_scope
from app.fireflies.pipeline import (
    _force_meeting_title_first_line,
    _strip_llm_todo_block,
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
            if not short.strip():
                continue
            # The short_summary is HTML-escaped (the title line is
            # wrapped in <a href>; the body is html.escape'd). To
            # apply _strip_llm_todo_block we need to peel off the
            # <a href> wrapper first, strip, and re-wrap.
            # Detect the wrapper shape: <a href="…">{first}</a>{rest}
            if short.startswith("<a href="):
                close_tag_idx = short.find("</a>")
                if close_tag_idx == -1:
                    skipped += 1
                    continue
                rest_escaped = short[close_tag_idx + len("</a>"):]
                # Decode HTML entities in rest so _strip can match
                # literal «To-Do:» / «TODO:» on the body.
                import html as _html
                rest_plain = _html.unescape(rest_escaped)
                stripped_plain = _strip_llm_todo_block(rest_plain)
                if stripped_plain.rstrip() == rest_plain.rstrip():
                    skipped += 1
                    continue
                # Rebuild: pull raw title + doc_url, then re-wrap
                # cleanly from scratch using the pipeline helpers
                # so the output is identical to what the live
                # pipeline produces. NOTE: «\n\n» (blank line)
                # between title and body — the live pipeline always
                # emits this separator, so the strip must preserve
                # it. `_force_meeting_title_first_line` swaps the
                # whole first line by splitting on the first «\n»,
                # so we keep the double newline by injecting it
                # right after the placeholder.
                stripped_full = (
                    "PLACEHOLDER_TITLE\n\n" + stripped_plain.lstrip()
                )
                stripped_full = _force_meeting_title_first_line(
                    stripped_full, r.title or "", r.meeting_date,
                )
                if r.google_doc_url:
                    stripped_full = _wrap_short_summary_with_doc_link(
                        stripped_full.rstrip(), r.google_doc_url,
                    )
            else:
                # Plain text path — no HTML wrap to peel.
                stripped_full = _strip_llm_todo_block(short)
                if stripped_full.rstrip() == short.rstrip():
                    skipped += 1
                    continue

            before_lines = len(short.split("\n"))
            after_lines = len(stripped_full.split("\n"))
            print(
                f"  ✓ {r.meeting_date.strftime('%m-%d %H:%M')} "
                f"[{src}] {(r.title or '')[:55]:<55} "
                f"lines {before_lines} → {after_lines}"
            )
            if not args.dry_run:
                r.short_summary = stripped_full
                session.flush()
            fixed += 1
        if not args.dry_run:
            session.commit()

    print()
    print(
        f"Stripped: {fixed}   "
        f"Already clean: {skipped}   "
        f"{'(dry-run, no commit)' if args.dry_run else 'COMMITTED'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
