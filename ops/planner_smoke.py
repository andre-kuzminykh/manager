#!/usr/bin/env python3
"""CLI smoke for CEO Brain planner — verifies that:

  1. The classifier picks the right MCPs for a real operator question.
  2. The planner produces a tool-call plan with NON-empty args.
  3. `search_fragment` is FORCED EMPTY for get_zoom_transcript /
     get_meeting (post-FR-CB2-3.38 hotfix — without this fix the bot
     answered «не нашёл реплик» on questions like «что Ира вчера
     говорила» because n8n's substring filter missed Irina /
     Шипилова variants).

Usage:
    docker compose exec -T bot python -m ops.planner_smoke
    docker compose exec -T bot python -m ops.planner_smoke \\
        --question "что вчера Артем говорил про EQT"
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from anthropic import Anthropic

from app.ceo_brain.config import get_mcp_servers
from app.ceo_brain.mcp_client import list_tools
from app.ceo_brain.planner import plan_tool_calls
from app.ceo_brain.responder import select_mcps_for_question
from app.ceo_brain.slack_tools import SLACK_TOOL_SCHEMAS
from app.config import get_settings


SLACK_SELF_URL = "local://slack"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--question", default="что Ира вчера говорила?",
        help="The exact operator question to plan against.",
    )
    args = ap.parse_args()

    s = get_settings()
    if not s.ceo_brain_anthropic_api_key:
        print("ERROR: CEO_BRAIN_ANTHROPIC_API_KEY not set.",
              file=sys.stderr)
        return 2
    client = Anthropic(
        api_key=s.ceo_brain_anthropic_api_key, timeout=60.0,
    )

    print("=" * 70)
    print(f"Question: {args.question!r}")
    print("=" * 70)

    mcp_servers = get_mcp_servers()
    servers_with_self = list(mcp_servers) + [
        {"name": "slack_self", "url": SLACK_SELF_URL, "type": "url"}
    ]
    print(f"\n[1/3] Classifier — picking MCPs from "
          f"{len(servers_with_self)} candidates…")
    picked = select_mcps_for_question(
        question=args.question,
        all_servers=servers_with_self,
        anthropic_client=client,
    )
    print(f"  → {[srv.get('name') for srv in picked]}")

    tools_by_mcp: dict = {}
    for srv in picked:
        name = srv.get("name") or ""
        url = srv.get("url") or ""
        if name == "slack_self":
            tools_by_mcp[name] = list(SLACK_TOOL_SCHEMAS)
        elif url:
            tools_by_mcp[name] = list_tools(url)
    print(f"  tool catalog sizes: "
          f"{[(k, len(v)) for k, v in tools_by_mcp.items()]}")

    print("\n[2/3] Planner — building the tool-call plan…")
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    plan = plan_tool_calls(
        question=args.question,
        mcp_servers=picked,
        tools_by_mcp=tools_by_mcp,
        anthropic_client=client,
        today_iso=today_iso,
    )
    print(f"  → {len(plan)} planned calls:")
    for c in plan:
        args_blob = json.dumps(c.get("args") or {}, ensure_ascii=False)
        print(f"    {c.get('mcp')}::{c.get('tool')}  args={args_blob}")

    print("\n[3/3] Sanity — search_fragment must be empty for transcript "
          "fetches…")
    bad: list[dict] = []
    for c in plan:
        tname = (c.get("tool") or "").lower()
        if tname in {"get_zoom_transcript", "get_meeting"}:
            sf = ((c.get("args") or {}).get("search_fragment") or "").strip()
            if sf:
                bad.append(c)
                print(f"  ❌ {tname} has non-empty search_fragment={sf!r}")
            else:
                print(f"  ✓ {tname} search_fragment is empty")
    if bad:
        print("\nFAIL: at least one transcript fetch carries a "
              "search_fragment filter — n8n's substring filter will "
              "miss name/term variants.")
        return 1
    has_transcript_call = any(
        (c.get("tool") or "").lower() in {"get_zoom_transcript", "get_meeting"}
        for c in plan
    )
    if not has_transcript_call:
        print("\n⚠️  Plan has NO transcript fetch — bot won't have the "
              "text to extract quotes from. Check that the classifier "
              "picked n8n_calendar and that the planner included "
              "get_zoom_transcript / get_meeting.")
        return 1
    print("\n✅ Plan looks correct; bot will get the full transcript.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
