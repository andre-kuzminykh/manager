"""Structured intent-extraction pipeline.

--- Spec ---

Input: a Slack message (source_text) plus up to 10 surrounding messages
(context, oldest first), plus the author's Slack user id and today's
local date.

Stage 1 — Detection (one LLM call, focused prompt):
    Question: is the source message a task?
    Output: {is_task, confidence, reasoning}.
    If is_task is false → pipeline returns no_action immediately.
    No further LLM calls are made.

Stage 2 — Parallel extraction (runs ONLY when Stage 1 said yes):
    2a. Title / description / priority — LLM call with title_prompt.
    2b. Owner — LLM call with owner_prompt. Sees the same 10-message
        context so it can distinguish "питчдек для Ивана" (audience)
        from "Иван, сделай питчдек" (assignee).
    2c. Due date — deterministic Python (date_resolver). No LLM.
    2a and 2b execute concurrently on a ThreadPoolExecutor so a small
    LLM's per-call latency doesn't stack.

Stage 3 — Assembly:
    Build IntentClassification(intent=create_task, confidence=<from
    Stage 1>, task=TaskDraft(<merged>)) and return.

Downstream UX routing (not part of this module, for reference):
    invocation_type=mention → auto-finalize regardless of confidence.
    invocation_type=passive + high confidence (>=0.75) → create task +
        admin review DM/ephemeral.
    invocation_type=passive + medium (>=0.40) → soft prompt in thread.
    invocation_type=passive + low → silent (unless the rules-based
        prefilter salvages it — see classify_with_backend).

Resilience:
    - A stage failing (exception, network, malformed output) does not
      abort the whole pipeline. Stage 1 failures return no_action.
      Stage 2a/b failures leave their respective fields null so the
      bot can ask the user later.
    - Stage 2c is deterministic and cannot fail.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date as _date
from typing import Any

from app.intent.date_resolver import resolve_due_date, strip_date_phrase
from app.intent.detect_prompt import (
    DETECT_SYSTEM_PROMPT,
    DETECT_TOOL_DESCRIPTION,
    DETECT_TOOL_NAME,
    DETECT_TOOL_PARAMETERS,
    build_detect_user_prompt,
)
from app.intent.owner_prompt import (
    OWNER_SYSTEM_PROMPT,
    OWNER_TOOL_DESCRIPTION,
    OWNER_TOOL_NAME,
    OWNER_TOOL_PARAMETERS,
    build_owner_user_prompt,
)
from app.intent.title_prompt import (
    TITLE_SYSTEM_PROMPT,
    TITLE_TOOL_DESCRIPTION,
    TITLE_TOOL_NAME,
    TITLE_TOOL_PARAMETERS,
    build_title_user_prompt,
)
from app.logging_setup import get_logger
from app.schemas.intent import IntentClassification, IntentType, TaskDraft

log = get_logger(__name__)


def _safe_call_tool(
    backend,
    *,
    system_prompt: str,
    user_prompt: str,
    tool_name: str,
    tool_description: str,
    tool_parameters: dict[str, Any],
) -> dict[str, Any] | None:
    """Wrap backend.call_tool so any failure returns None instead of
    aborting the whole pipeline."""
    try:
        out = backend.call_tool(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tool_name=tool_name,
            tool_description=tool_description,
            tool_parameters=tool_parameters,
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "pipeline_stage_failed",
            tool=tool_name,
            error=str(e),
        )
        return None
    return out if isinstance(out, dict) else None


def run_pipeline(
    *,
    backend,
    source_text: str,
    context_messages: list[dict],
    author_user_id: str | None,
    today: _date,
) -> IntentClassification:
    # Stage 1: Detection.
    detect = _safe_call_tool(
        backend,
        system_prompt=DETECT_SYSTEM_PROMPT,
        user_prompt=build_detect_user_prompt(
            source_text=source_text, context_messages=context_messages
        ),
        tool_name=DETECT_TOOL_NAME,
        tool_description=DETECT_TOOL_DESCRIPTION,
        tool_parameters=DETECT_TOOL_PARAMETERS,
    )
    if not detect or not detect.get("is_task"):
        return IntentClassification(
            intent=IntentType.no_action,
            confidence=float(detect.get("confidence", 0.0)) if detect else 0.0,
            reasoning=(detect or {}).get("reasoning"),
        )

    confidence = float(detect.get("confidence", 0.75))

    # Stage 2: Parallel extraction.
    def _run_title() -> dict[str, Any] | None:
        return _safe_call_tool(
            backend,
            system_prompt=TITLE_SYSTEM_PROMPT,
            user_prompt=build_title_user_prompt(
                source_text=source_text, context_messages=context_messages
            ),
            tool_name=TITLE_TOOL_NAME,
            tool_description=TITLE_TOOL_DESCRIPTION,
            tool_parameters=TITLE_TOOL_PARAMETERS,
        )

    def _run_owner() -> dict[str, Any] | None:
        return _safe_call_tool(
            backend,
            system_prompt=OWNER_SYSTEM_PROMPT,
            user_prompt=build_owner_user_prompt(
                source_text=source_text,
                context_messages=context_messages,
                author_user_id=author_user_id,
            ),
            tool_name=OWNER_TOOL_NAME,
            tool_description=OWNER_TOOL_DESCRIPTION,
            tool_parameters=OWNER_TOOL_PARAMETERS,
        )

    with ThreadPoolExecutor(max_workers=2) as ex:
        title_future = ex.submit(_run_title)
        owner_future = ex.submit(_run_owner)
    title_data = title_future.result() or {}
    owner_data = owner_future.result() or {}
    due = resolve_due_date(source_text, today)

    # Stage 3: Assembly.
    # Dates belong in due_date, never in the title — strip any trailing
    # "к 1 мая" / "ко вторнику" the LLM may have left behind.
    title = strip_date_phrase(
        (title_data.get("title") or source_text[:120]).strip()
    ) or "(untitled)"
    description = title_data.get("description") or None
    priority = title_data.get("priority") or "medium"
    slack_user_id = owner_data.get("slack_user_id") or None
    display_name = owner_data.get("display_name") or None

    return IntentClassification(
        intent=IntentType.create_task,
        confidence=confidence,
        reasoning=detect.get("reasoning"),
        task=TaskDraft(
            title=title,
            description=description,
            priority=priority,
            owner_user_id=slack_user_id,
            owner_display_name=display_name,
            due_date=due,
        ),
    )


__all__ = ["run_pipeline"]
