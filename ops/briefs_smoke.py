#!/usr/bin/env python3
"""FR-CR-05-171 CLI smoke — verify briefs beneficiary picker no
longer drops named meeting attendees in favour of company CEOs.

Operator's regression: «Baris Yildiz (Apple) <> Artem Sokolov
(Humanoid)» produced a brief about Tim Cook (Apple CEO) and nothing
about Baris — wrong human, real meeting attendee silently dropped.

This smoke runs the actual Stage-2 picker (`extract_beneficiaries`)
with a synthetic Calendar event matching the operator's case and
checks: (a) Baris IS in the output; (b) Tim Cook can be there too
as a leadership top-up; (c) Baris is FIRST (seed > LLM picks).

Usage:
    docker compose exec -T bot python -m ops.briefs_smoke
    docker compose exec -T bot python -m ops.briefs_smoke \\
        --person "Baris Yildiz" --org Apple
"""
from __future__ import annotations

import argparse
import sys

from app.config import get_settings
from app.counterparty_briefs.extract import (
    extract_beneficiaries,
    extract_event_counterparties,
)
from app.counterparty_briefs.research import OrgResearch, research_org
from app.intent.llm_backends import OpenAIBackend


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--person", default="Baris Yildiz")
    ap.add_argument("--org", default="Apple")
    ap.add_argument(
        "--max-n", type=int, default=5,
        help="MAX_BENEFICIARIES — how many people to research per event.",
    )
    ap.add_argument(
        "--skip-research", action="store_true",
        help="Don't make the org-research LLM call (use stub leadership).",
    )
    args = ap.parse_args()

    s = get_settings()
    if not s.openai_api_key:
        print("ERROR: OPENAI_API_KEY not set.", file=sys.stderr)
        return 2
    llm = OpenAIBackend(api_key=s.openai_api_key)

    print("=" * 70)
    print(f"Person: {args.person}")
    print(f"Org:    {args.org}")
    print(f"max_n:  {args.max_n}")
    print("=" * 70)

    # Synthesize the operator's exact Calendar shape.
    event = {
        "id": "evt-smoke",
        "title": f"{args.person} ({args.org}) ‹› Artem Sokolov (Humanoid)",
        "description": (
            f"Intro call with {args.person}, "
            f"{args.org}. https://zoom.us/j/12345"
        ),
        "attendees": [
            {"email": "1@thehumanoid.ai",
             "displayName": "Artem Sokolov",
             "responseStatus": "accepted"},
            # No external email — operator's actual case: name is in
            # the title but the Calendar invite was only to internals.
        ],
    }

    print("\n[1/3] Stage 0 — extract_event_counterparties on the event…")
    ex = extract_event_counterparties(
        event=event,
        llm_backend=llm,
        model=s.counterparty_briefs_extract_model,
    )
    print(f"  org_name:        {ex.org_name!r}")
    print(f"  initial_persons: {[p.person_name for p in ex.initial_persons]}")
    if not ex.initial_persons:
        print("\n⚠️  Stage 0 returned NO initial_persons. FR-CR-05-171 seed "
              "depends on this — Baris won't be in beneficiaries.")
        print("    Check the title contains the name in an LLM-legible form.")

    print("\n[2/3] Org research (leadership list)…")
    if args.skip_research:
        leadership = [
            {"name": "Tim Cook", "role": "CEO"},
            {"name": "Luca Maestri", "role": "CFO"},
            {"name": "Eddy Cue", "role": "SVP Services"},
        ]
        org_research = OrgResearch(name=ex.org_name or args.org,
                                   leadership=leadership)
        print(f"  (stub) leadership: {[m['name'] for m in leadership]}")
    else:
        try:
            org_research = research_org(
                org_name=ex.org_name or args.org,
                llm_backend=llm,
                model=s.counterparty_briefs_research_model,
                budget_usd=float(s.counterparty_briefs_llm_budget_usd),
            )
        except Exception as e:  # noqa: BLE001
            print(f"  research_org failed ({e}); using stub.")
            org_research = OrgResearch(
                name=ex.org_name or args.org,
                leadership=[{"name": "Tim Cook", "role": "CEO"}],
            )
        if org_research is None:
            org_research = OrgResearch(
                name=ex.org_name or args.org,
                leadership=[{"name": "Tim Cook", "role": "CEO"}],
            )
        names = [(m or {}).get("name") for m in
                 (org_research.leadership or [])]
        print(f"  leadership: {names}")

    print("\n[3/3] Stage 2 — extract_beneficiaries (the FR-CR-05-171 path)…")
    out = extract_beneficiaries(
        org_research=org_research,
        attendees=event["attendees"],
        initial_persons=ex.initial_persons,
        max_n=args.max_n,
        llm_backend=llm,
        model=s.counterparty_briefs_extract_model,
    )
    names = [b.person_name for b in out]
    print(f"  → {len(out)} beneficiaries: {names}")
    print()
    for b in out:
        print(f"    - {b.person_name:30s} role={b.person_role!s:20s} "
              f"evidence={b.evidence[:50]!r}")

    print("\n=== Verdict ===")
    ok = True
    if args.person not in names:
        print(f"❌ FAIL: {args.person!r} NOT in beneficiaries.")
        ok = False
    else:
        idx = names.index(args.person)
        print(f"✅ {args.person!r} found at position {idx + 1} of {len(names)}")
        if idx != 0:
            print(f"⚠️  Expected position 1 (seed), got {idx + 1}.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
