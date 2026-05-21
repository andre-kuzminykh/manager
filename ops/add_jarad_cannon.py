"""FR-CR-05-192e — seed Jarad Cannon (CTO) into TeamMember and
fix the leftover «Jared Kinnan» mis-transcription in stored
summaries.

The LLM consistently mis-transcribes «Jarad Cannon» as «Jared
Kinnan» on the 19/05 - Object First / Dutchess Management call.
The canonicalize matcher requires the surname token to match
between mention and canonical (FR-CR-05-191 v3) — «kinnan» ≠
«cannon», so it never gets rewritten.

Two-part fix:

  1. INSERT TeamMember(real_name='Jarad Cannon', role='CTO',
     active=true) so future detections + the People-dashboard
     pick him up.

  2. UPDATE short_summary + detailed_summary for any record where
     «Jared Kinnan» (or close phonetic variants) appears →
     «Jarad Cannon».

Idempotent — running twice is a no-op.

Usage:
    docker exec manager-bot-1 python -m ops.add_jarad_cannon [--dry-run]
"""
from __future__ import annotations

import argparse
import re
import sys

from app.db import session_scope
from app.models import MeetingRecording, TeamMember, ZoomRecording


# Phonetic variants the Whisper / pipeline LLM has produced.
# Word-boundary anchored so we don't smash things like
# «канон» inside a longer word.
BAD_NAME_PATTERNS = [
    re.compile(r"\bJared\s+Kinnan\b", re.IGNORECASE),
    re.compile(r"\bJared\s+Kennan\b", re.IGNORECASE),
    re.compile(r"\bJared\s+Cannon\b", re.IGNORECASE),  # right surname, wrong first name
    re.compile(r"\bДжаред\s+Киннан\b", re.IGNORECASE),
    re.compile(r"\bДжаред\s+Кеннан\b", re.IGNORECASE),
    re.compile(r"\bДжаред\s+Кэннон\b", re.IGNORECASE),
]
CANONICAL = "Jarad Cannon"


def _fix(text: str | None) -> tuple[str, int]:
    if not text:
        return text or "", 0
    out = text
    n = 0
    for pat in BAD_NAME_PATTERNS:
        new_out, hits = pat.subn(CANONICAL, out)
        if hits:
            n += hits
            out = new_out
    return out, n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with session_scope() as s:
        # ---- 1. Seed Jarad Cannon ---- #
        existing = (
            s.query(TeamMember)
            .filter(TeamMember.real_name == CANONICAL)
            .first()
        )
        if existing is None:
            m = TeamMember(
                real_name=CANONICAL,
                role="CTO",
                active=True,
                notes=(
                    "Seeded 2026-05-21 via ops/add_jarad_cannon.py. "
                    "External CTO encountered on 19/05 Object First / "
                    "Dutchess Management call. Whisper consistently "
                    "mis-transcribes as «Jared Kinnan»."
                ),
            )
            if not args.dry_run:
                s.add(m)
                s.flush()
                print(f"  ✓ Inserted TeamMember id={m.id} real_name='{CANONICAL}' role=CTO")
            else:
                print(f"  ✓ (dry-run) Would insert TeamMember real_name='{CANONICAL}' role=CTO")
        else:
            print(f"  · TeamMember '{CANONICAL}' already exists (id={existing.id}, role={existing.role}) — no insert")

        # ---- 2. Patch summaries ---- #
        total_hits = 0
        records_patched = 0
        for r in (
            s.query(ZoomRecording).all()
            + s.query(MeetingRecording).all()
        ):
            new_short, n1 = _fix(r.short_summary)
            new_detail, n2 = _fix(r.detailed_summary)
            if n1 + n2 == 0:
                continue
            rid = getattr(r, "zoom_id", None) or getattr(r, "fireflies_id", None)
            print(
                f"  ✓ {r.meeting_date.strftime('%Y-%m-%d %H:%M')} "
                f"[{rid}] {(r.title or '')[:55]} — "
                f"short={n1} detailed={n2}"
            )
            total_hits += n1 + n2
            records_patched += 1
            if not args.dry_run:
                if n1:
                    r.short_summary = new_short
                if n2:
                    r.detailed_summary = new_detail
                s.flush()
        if not args.dry_run:
            s.commit()
        print()
        print(
            f"Records patched: {records_patched}  "
            f"Total rewrites: {total_hits}  "
            f"{'(dry-run, no commit)' if args.dry_run else 'COMMITTED'}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
