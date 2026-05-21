"""FR-CR-05-191b — Seed Counterparty table from extracted
organization mentions across recent meeting summaries.

Current Counterparty table has only 16 Type-categorized rows
(Financial/VC, Government, etc.) and is missing the actual
companies (CDIB, Affinity, Bosch, Foxconn, Aramco, etc.) that
appear in every meeting. This blocks `canonicalize_summary_text`
org-name rewriting because the directory is empty of real names.

This script reads `detailed_summary` from each Zoom + Fireflies
record in the date range, runs the LLM entity extractor to pull
out organization names, and find-or-creates a Counterparty hub
row for each unique normalised name. NO touching of existing
rows — purely additive.

Usage:
    docker exec manager-bot-1 python -m ops.seed_counterparties_from_summaries \\
        --start 2026-05-19 --end 2026-05-22 [--dry-run] [--min-occurrences 1]
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from app.config import get_settings
from app.db import session_scope
from app.intent.llm_backends import OpenAIBackend
from app.models import MeetingRecording, ZoomRecording
from app.models.counterparty import Counterparty
from app.services.summary_canonicalize import extract_name_entities
from app.sync.counterparties import normalise_name


_TOO_GENERIC = {
    "humanoid", "humain", "company", "fund", "investor",
    "investors", "bank", "banks", "government", "ventures",
    "capital", "partners", "advisors", "team", "office",
}


def _is_generic_or_short(name: str) -> bool:
    n = (name or "").strip()
    if len(n) < 3:
        return True
    norm = normalise_name(n) or ""
    if not norm:
        return True
    if norm in _TOO_GENERIC:
        return True
    return False


def _extract_for_row(record_id: str, text: str, llm, model: str) -> list[str]:
    """Worker: pull org mentions from one summary."""
    if not text or not text.strip():
        return []
    try:
        ents = extract_name_entities(text, llm_backend=llm, model=model)
    except Exception:  # noqa: BLE001
        return []
    return ents.get("organizations") or []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--min-occurrences", type=int, default=1,
        help="Only add a company if it appears in N+ summaries "
             "(default 1, dedupes one-off mentions).",
    )
    ap.add_argument(
        "--workers", type=int, default=6,
    )
    args = ap.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)

    s = get_settings()
    from openai import OpenAI
    oc = OpenAI(api_key=s.openai_api_key)
    llm = OpenAIBackend(client=oc, model=s.fireflies_tasks_model)
    model = s.fireflies_tasks_model

    # Collect (recording_id, detailed_summary) pairs
    pairs: list[tuple[str, str]] = []
    with session_scope() as session:
        for r in session.query(ZoomRecording).filter(
            ZoomRecording.meeting_date >= start,
            ZoomRecording.meeting_date < end,
        ).all():
            if (r.detailed_summary or "").strip():
                pairs.append((r.zoom_id, r.detailed_summary))
        for r in session.query(MeetingRecording).filter(
            MeetingRecording.meeting_date >= start,
            MeetingRecording.meeting_date < end,
        ).all():
            if (r.detailed_summary or "").strip():
                pairs.append((r.fireflies_id, r.detailed_summary))
    print(f"Records with detailed_summary: {len(pairs)}")

    # Parallel extract
    mention_counts: dict[str, int] = defaultdict(int)
    mention_examples: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {
            ex.submit(_extract_for_row, rid, txt, llm, model): rid
            for rid, txt in pairs
        }
        done = 0
        for fut in as_completed(futs):
            done += 1
            rid = futs[fut]
            try:
                orgs = fut.result()
            except Exception as e:  # noqa: BLE001
                print(f"  [{done}/{len(pairs)}] ERR {rid}: {e}")
                continue
            seen = set()
            for o in orgs:
                norm = normalise_name(o) or ""
                if not norm or _is_generic_or_short(o):
                    continue
                if norm in seen:
                    continue  # same mention in same record counts once
                seen.add(norm)
                mention_counts[norm] += 1
                if norm not in mention_examples:
                    mention_examples[norm] = o.strip()
            print(f"  [{done}/{len(pairs)}] {rid}: +{len(orgs)} mentions")

    # Filter by min-occurrences
    eligible = {
        norm: mention_examples[norm]
        for norm, cnt in mention_counts.items()
        if cnt >= args.min_occurrences
    }
    print()
    print(f"Unique normalised mentions: {len(mention_counts)}")
    print(f"Eligible (≥ {args.min_occurrences} occurrences): {len(eligible)}")

    # Find-or-create in Counterparty
    with session_scope() as session:
        existing_norms = {
            n for (n,) in session.query(Counterparty.name_normalised).all()
        }
        to_add = {
            n: name for n, name in eligible.items()
            if n not in existing_norms
        }
        print(f"Already in Counterparty: "
              f"{len(eligible) - len(to_add)}")
        print(f"NEW to add: {len(to_add)}")
        print()
        # Sort by occurrence count desc for readable output
        sorted_add = sorted(
            to_add.items(),
            key=lambda kv: -mention_counts[kv[0]],
        )
        for norm, name in sorted_add[:50]:
            cnt = mention_counts[norm]
            print(f"  +{cnt:>3} {name:<40}  (norm: {norm})")
        if len(sorted_add) > 50:
            print(f"  ... and {len(sorted_add) - 50} more")
        if not args.dry_run:
            for norm, name in to_add.items():
                cp = Counterparty(name=name, name_normalised=norm)
                session.add(cp)
            session.commit()
            print()
            print(f"COMMITTED: {len(to_add)} new Counterparty rows")
    if args.dry_run:
        print()
        print("--dry-run — no commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
