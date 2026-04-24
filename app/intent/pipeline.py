"""Structured intent-extraction pipeline.

--- Spec (updated 2026-04-24) ---

Input: a Slack message (source_text) plus up to 10 surrounding
messages (context, oldest first), plus the author's Slack user id and
today's local date. Voice-note attachments on the Slack message are
transcribed via Whisper in the handler layer
(app/services/transcription.py); the transcript is appended to the
caption (if any) before this pipeline sees the text.

Stage 1 — Detection (one focused LLM call):
    Question: "is the source message a task?"
    Output: {is_task, confidence, reasoning}.
    If is_task is false → the pipeline returns no_action immediately.
    No further LLM calls are made.

Stage 2 — Parallel extraction (runs only when Stage 1 said yes):
    2a. Title / description / priority — LLM call with title_prompt.
        After the call both title and description are piped through
        strip_date_phrase() so the date ends up ONLY in due_date.
    2b. Owner — LLM call with owner_prompt. Sees the same 10-message
        context so it can distinguish "питчдек для Ивана" (audience)
        from "Иван, сделай питчдек" (assignee).
    2c. Due date — deterministic Python (date_resolver). No LLM.
    2a and 2b run concurrently on a ThreadPoolExecutor so a small
    LLM's per-call latency doesn't stack.

Stage 3 — Assembly: build IntentClassification(intent=create_task, …).

Downstream UX contract (implemented in the Slack handlers):
    @mention → task is created immediately (no Accept button). If the
              resulting Task is missing a user-visible field (owner,
              due_date, description, effort) OR the owner is only a
              fallback to the message author (owner_assumed), the bot
              posts a follow-up question in the source thread. The
              thread reply updates the Task and refreshes the card in
              place. (handled in handle_app_mention +
              _handle_followup_reply)
    passive → bot posts a PRE-FILLED draft card with three buttons:
              Accept / Edit / Reject. At the same time it posts the
              missing-field question in the thread (same machinery as
              @mention, but for draft.payload). The user can answer in
              chat OR open Edit; either way the card refreshes. On
              Accept the draft becomes a Task; any remaining gap
              triggers another follow-up round.

Resilience:
    - A stage failing (exception, network, malformed output) does not
      abort the whole pipeline. Stage 1 failures → no_action. Stage 2a
      / 2b failures leave their fields null; the follow-up loop
      fills them later.
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
    # Dates belong in due_date, never in the title or the description —
    # strip any trailing "к 1 мая" / "ко вторнику" the LLM may have
    # smuggled into either field.
    title = strip_date_phrase(
        (title_data.get("title") or source_text[:120]).strip()
    ) or "(untitled)"
    raw_description = title_data.get("description") or None
    if isinstance(raw_description, str):
        cleaned = strip_date_phrase(raw_description.strip())
        description = cleaned or None
    else:
        description = raw_description
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
