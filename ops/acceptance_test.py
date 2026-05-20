#!/usr/bin/env python3
"""FR-CB2-Acceptance — end-to-end acceptance suite.

Runs each of the operator's typical questions through the full
pipeline (classifier → planner → gather → synthesize) and prints
PASS/FAIL with diagnostic detail. Single command, no Slack
involvement.

Usage:
    docker compose exec -T bot python -m ops.acceptance_test

Each case prints:
  - The question text
  - Classifier picks (which MCPs were chosen)
  - Planner output (which tools + args)
  - Gather summary (which labels returned non-empty data)
  - Synthesis (first 600 chars)
  - PASS / FAIL with reason
"""
from __future__ import annotations

import sys
import time
import traceback
from datetime import datetime, timezone

from anthropic import Anthropic

from app.ceo_brain.config import get_mcp_servers
from app.ceo_brain.mcp_client import list_tools as _list_tools
from app.ceo_brain.parallel_gather import (
    gather_via_direct_http,
    synthesize_final_answer,
)
from app.ceo_brain.planner import plan_tool_calls
from app.ceo_brain.responder import (
    build_system_prompt,
    select_mcps_for_question,
)
from app.ceo_brain.slack_tools import (
    SLACK_TOOL_SCHEMAS,
    build_executors,
)
from app.config import get_settings


SLACK_SELF_URL = "local://slack"

# Acceptance cases: (label, question, expected_mcp_substring_or_None).
# expected_mcp is just a hint — pass = answer non-empty, not "не
# сформулировала", and gathered something useful.
_CASES: list[tuple[str, str, str | None]] = [
    (
        "1.1 Внутренний Zoom — реплики Иры",
        "что Ира вчера сказала на встрече по фандрайзингу",
        "n8n_calendar",
    ),
    (
        "1.2 Внутренний Zoom — суть сегодня",
        "что обсудили на синке сегодня",
        "n8n_calendar",
    ),
    (
        "1.3 Задачи по встрече",
        "какие задачи по Fundraising daily на этой неделе",
        "n8n_calendar",
    ),
    (
        "2 Внешняя встреча с Йоханом",
        "что обсудили с Йоханом на встрече с инвесторами",
        "n8n_calendar",
    ),
    (
        "3.1 Telegram — Алина сегодня",
        "что писала Алина сегодня в телеграме",
        "n8n_drive",
    ),
    (
        "3.2 Telegram — список чатов",
        "какие чаты есть в моём телеграме",
        "n8n_drive",
    ),
    (
        "4 Google Drive",
        "найди документ про term sheet",
        "n8n_main",
    ),
    (
        "5 LinkedIn",
        "найди в LinkedIn Sam Altman",
        "n8n_gmail",
    ),
    (
        "6 RocketReach",
        "найди контакты Mohammed Al Fardan",
        "n8n_rocketreach",
    ),
    (
        "7 HubSpot",
        "что есть в hubspot по компании Humanoid",
        "n8n_hubspot",
    ),
    (
        "8 Slack-self",
        "в каких каналах ты добавлен",
        "slack_self",
    ),
    (
        "9 Multi-part",
        "что обсудили с Йоханом на встрече сегодня, "
        "и что Алина писала в телеграм",
        None,  # хотим оба источника
    ),
    (
        "10 Follow-up — context resolution",
        # FR-CB2-3.37 — this is asked in a synthetic thread after a
        # prior turn that referenced Irina. Acceptance harness
        # supplies thread_history with prior user/assistant exchange.
        "а за вчера?",
        None,
    ),
    (
        "11 Follow-up — meeting drill-down",
        "а на встречах что Ира говорила сегодня",
        "n8n_calendar",
    ),
]


_RED_FLAGS = (
    "не сформулировала",
    "временная ошибка",
    "schema error",
    "ошибка схемы",
)


def _run_one(
    *,
    client: Anthropic,
    question: str,
    all_servers_with_self: list[dict],
    system_prompt: str,
    slack_self_executors: dict | None,
    thread_context: list[dict] | None = None,
) -> dict:
    """Run one question end-to-end. Returns dict with diagnostic
    fields + pass/fail."""
    t0 = time.time()
    out: dict = {
        "question": question,
        "elapsed_sec": 0.0,
        "picked": [],
        "planned": [],
        "gathered_non_empty": [],
        "gathered_empty": [],
        "synth_chars": 0,
        "synth_preview": "",
        "pass": False,
        "fail_reason": "",
    }
    try:
        picked = select_mcps_for_question(
            question=question,
            all_servers=all_servers_with_self,
            anthropic_client=client,
            thread_context=thread_context,
        )
        out["picked"] = [s.get("name") for s in picked]

        tools_by_mcp: dict = {}
        for srv in picked:
            name = srv.get("name") or ""
            url = srv.get("url") or ""
            if name == "slack_self":
                tools_by_mcp[name] = list(SLACK_TOOL_SCHEMAS)
            elif url:
                tools_by_mcp[name] = _list_tools(url)
        today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        plan = plan_tool_calls(
            question=question,
            mcp_servers=picked,
            tools_by_mcp=tools_by_mcp,
            anthropic_client=client,
            today_iso=today_iso,
            thread_context=thread_context,
        )
        out["planned"] = [
            {
                "mcp": c.get("mcp"),
                "tool": c.get("tool"),
                "args": c.get("args"),
            }
            for c in plan
        ]
        if not plan:
            out["fail_reason"] = "planner_returned_empty"
            return out

        gathered = gather_via_direct_http(
            planned_calls=plan,
            mcp_servers=picked,
            tools_by_mcp=tools_by_mcp,
            local_tool_executors=slack_self_executors,
        )
        out["gathered_non_empty"] = [
            f"{k} ({len(v)}ch)" for k, v in gathered.items() if v
        ]
        out["gathered_empty"] = [
            k for k, v in gathered.items() if not v
        ]
        if not any(gathered.values()):
            out["fail_reason"] = "all_gathered_empty"
            return out

        final = synthesize_final_answer(
            question=question,
            gathered=gathered,
            anthropic_client=client,
            system_prompt_text=system_prompt,
        )
        out["synth_chars"] = len(final or "")
        out["synth_preview"] = (final or "")[:400]
        low = (final or "").lower()
        if any(f in low for f in _RED_FLAGS):
            out["fail_reason"] = f"red_flag_in_synth: {final[:200]}"
            return out
        if not (final or "").strip():
            out["fail_reason"] = "empty_synthesis"
            return out
        out["pass"] = True
    except Exception as e:  # noqa: BLE001
        out["fail_reason"] = (
            f"exception: {type(e).__name__}: {e}\n"
            + traceback.format_exc()
        )
    finally:
        out["elapsed_sec"] = round(time.time() - t0, 2)
    return out


def main() -> int:
    settings = get_settings()
    if not settings.ceo_brain_anthropic_api_key:
        print("ERROR: CEO_BRAIN_ANTHROPIC_API_KEY not set.")
        return 2
    client = Anthropic(
        api_key=settings.ceo_brain_anthropic_api_key,
        timeout=90.0,
    )
    mcp_servers = get_mcp_servers()
    if not mcp_servers:
        print("ERROR: MCP_SERVERS empty.")
        return 2
    # Inject slack_self virtual MCP so 8.x case can route locally.
    servers_with_self = list(mcp_servers) + [
        {"name": "slack_self", "url": SLACK_SELF_URL, "type": "url"}
    ]
    # Local Slack tools — we don't have a live SlackClient here, so
    # slack_self cases will fail gracefully (executor missing).
    # If you want to exercise slack_self too, pass a WebClient
    # via env (out of scope for a CLI test).
    slack_self_executors = None
    try:
        from slack_sdk import WebClient
        token = (
            settings.ceo_brain_slack_bot_token
            or settings.agenda_slack_bot_token
            or settings.slack_bot_token
        )
        if token:
            bot_client = WebClient(token=token)
            user_token = (
                settings.ceo_brain_slack_user_token or ""
            ).strip()
            user_client = WebClient(token=user_token) if user_token else None
            slack_self_executors = build_executors(
                bot_client=bot_client, user_client=user_client,
            )
    except Exception as e:  # noqa: BLE001
        print(f"(slack_self executors unavailable: {e})")

    system_prompt = build_system_prompt()
    passed = 0
    failed = 0
    print("=" * 70)
    print("CEO Brain Acceptance Suite")
    print("=" * 70)
    # FR-CB2-3.37 — synthetic thread context for follow-up cases.
    # When the question is a follow-up («а за вчера», «а на
    # встречах что Ира говорила сегодня»), seed prior turns so
    # classifier+planner can resolve the implicit reference.
    follow_up_seed = [
        {"role": "user",
         "content": "что сегодня сказала Ира на встрече?"},
        {"role": "assistant",
         "content": "Ира сегодня говорила на Fundraising daily про "
                    "investor pipeline: статус EQT, фильтрация "
                    "таблицы инвесторов, новые имена."},
    ]
    follow_up_labels = {
        "10 Follow-up — context resolution",
        "11 Follow-up — meeting drill-down",
    }

    for label, question, hint in _CASES:
        print(f"\n[{label}]")
        print(f"  Q: {question}")
        if hint:
            print(f"  hint: expect to use {hint}")
        thread_ctx = (
            follow_up_seed if label in follow_up_labels else None
        )
        if thread_ctx:
            print(f"  thread_context: {len(thread_ctx)} prior turns")
        result = _run_one(
            client=client, question=question,
            all_servers_with_self=servers_with_self,
            system_prompt=system_prompt,
            slack_self_executors=slack_self_executors,
            thread_context=thread_ctx,
        )
        print(f"  ⏱ {result['elapsed_sec']}s")
        print(f"  picked: {result['picked']}")
        if result["planned"]:
            for p in result["planned"]:
                print(f"    {p['mcp']}::{p['tool']} args={p['args']}")
        if result["gathered_non_empty"]:
            print(f"  ✓ non-empty: {result['gathered_non_empty']}")
        if result["gathered_empty"]:
            print(f"  ∅ empty: {result['gathered_empty']}")
        if result["pass"]:
            passed += 1
            print(f"  PASS — synth ({result['synth_chars']} chars):")
            print(f"    {result['synth_preview'][:200]}")
        else:
            failed += 1
            print(f"  ❌ FAIL — {result['fail_reason']}")
            if result["synth_preview"]:
                print(f"    synth preview: {result['synth_preview'][:200]}")
    print("\n" + "=" * 70)
    print(f"PASSED: {passed} / {len(_CASES)}")
    print(f"FAILED: {failed} / {len(_CASES)}")
    print("=" * 70)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
