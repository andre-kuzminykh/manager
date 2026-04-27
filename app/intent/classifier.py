from __future__ import annotations

import json
from datetime import date
from typing import Any

from app.config import get_settings
from app.context.retriever import ContextWindow
from app.intent.llm_backends import (
    INTENT_TOOL_DESCRIPTION,
    INTENT_TOOL_NAME,
    INTENT_TOOL_PARAMETERS,
    AnthropicBackend,
    LLMBackend,
    OpenAIBackend,
)
from app.intent.date_resolver import resolve_due_date, strip_date_phrase
from app.intent.pipeline import run_pipeline
from app.intent.prompts import SYSTEM_PROMPT, build_user_prompt
from app.intent.rules import prefilter_intent
from app.logging_setup import get_logger
from app.schemas.intent import IntentClassification, IntentType, InvocationType

log = get_logger(__name__)

# Kept for back-compat with tests that import the schema directly.
_INTENT_TOOL = {
    "name": INTENT_TOOL_NAME,
    "description": INTENT_TOOL_DESCRIPTION,
    "input_schema": INTENT_TOOL_PARAMETERS,
}


class IntentClassifier:
    """Composes rule prefilter + LLM extraction through a pluggable backend."""

    def __init__(
        self,
        anthropic_client: Any | None = None,
        *,
        backend: LLMBackend | None = None,
    ) -> None:
        # New path: pass an already-constructed backend (Anthropic or OpenAI).
        # Old path: pass an Anthropic SDK client — we wrap it automatically so
        # existing callers and tests keep working.
        self._settings = get_settings()
        if backend is not None:
            self._backend: LLMBackend | None = backend
        elif anthropic_client is not None:
            self._backend = AnthropicBackend(
                anthropic_client, self._settings.anthropic_model
            )
        else:
            self._backend = None

    @property
    def backend(self) -> LLMBackend | None:
        return self._backend

    def classify(
        self,
        *,
        context: ContextWindow,
        invocation_type: InvocationType,
        known_employees: list[dict] | None = None,
    ) -> IntentClassification:
        source_text = (context.source_message.get("text") or "").strip()

        if self._backend is None:
            # Without an LLM we can only return the coarse rule hint.
            prefilter = prefilter_intent(source_text)
            return IntentClassification(
                intent=prefilter.hint,
                confidence=prefilter.score,
                reasoning="prefilter only (no LLM configured)",
            )

        # Always run the pipeline, including on passive messages — the
        # prefilter is not exhaustive (it misses "нам нужно починить X"
        # and similar), and its Stage-1 detection is cheap enough on
        # gpt-4o-mini that we'd rather over-call than miss a real task.
        return classify_with_backend(
            backend=self._backend,
            context=context,
            invocation_type=invocation_type,
            source_text=source_text,
            known_employees=known_employees,
        )


def classify_with_backend(
    *,
    backend: LLMBackend,
    context: ContextWindow,
    invocation_type: InvocationType,
    source_text: str,
    known_employees: list[dict] | None = None,
) -> IntentClassification:
    try:
        date_model = (get_settings().openai_date_model or None)
        classification = run_pipeline(
            backend=backend,
            source_text=source_text,
            context_messages=context.flat_messages(),
            author_user_id=context.source_message.get("user"),
            today=date.today(),
            date_model=date_model,
            known_employees=known_employees,
        )
    except Exception as e:  # noqa: BLE001 — degrade to rules
        log.error("intent_pipeline_failed", error=str(e))
        pf = prefilter_intent(source_text)
        return IntentClassification(
            intent=pf.hint,
            confidence=pf.score * 0.5,
            reasoning=f"pipeline error, fell back to rules: {e!s}",
        )

    # Rule-based safety net: small LLMs occasionally return no_action
    # for unambiguous task phrases ("надо подготовить заметки к 1 мая").
    # If the prefilter saw a strong task/meeting signal, synthesise a
    # minimal draft from the source text. The prefilter score (0.55)
    # keeps us in the soft-prompt bucket, not in auto-create.
    if classification.intent == IntentType.no_action:
        pf = prefilter_intent(source_text)
        if pf.hint != IntentType.no_action:
            from app.schemas.intent import MeetingDraft, TaskDraft

            title = strip_date_phrase(source_text[:200]) or source_text[:200]
            if pf.hint in (IntentType.create_task, IntentType.update_task):
                classification = IntentClassification(
                    intent=IntentType.create_task,
                    confidence=pf.score,
                    task=TaskDraft(
                        title=title,
                        due_date=resolve_due_date(source_text, date.today()),
                    ),
                    reasoning=(
                        "prefilter override: pipeline said no_action but "
                        "rules matched task keywords"
                    ),
                )
            elif pf.hint in (IntentType.create_meeting, IntentType.update_meeting):
                classification = IntentClassification(
                    intent=IntentType.create_meeting,
                    confidence=pf.score,
                    meeting=MeetingDraft(title=title),
                    reasoning=(
                        "prefilter override: pipeline said no_action but "
                        "rules matched meeting keywords"
                    ),
                )
    return classification


# -- Back-compat wrappers kept for existing tests --------------------------


def classify_with_llm(
    *,
    client: Any,
    model: str,
    context: ContextWindow,
    invocation_type: InvocationType,
    source_text: str,
) -> IntentClassification:
    """Legacy entrypoint: Anthropic client → AnthropicBackend."""
    backend = AnthropicBackend(client, model)
    return classify_with_backend(
        backend=backend,
        context=context,
        invocation_type=invocation_type,
        source_text=source_text,
    )


def _extract_tool_input(response: Any) -> dict[str, Any] | None:
    """Legacy helper used by classifier tests (Anthropic-style response)."""
    from app.intent.llm_backends import _extract_anthropic_tool_input

    return _extract_anthropic_tool_input(response)


def _parse_classification(data: dict[str, Any]) -> IntentClassification:
    try:
        return IntentClassification.model_validate(data)
    except Exception as e:  # noqa: BLE001
        log.warning("intent_parse_failed", error=str(e), data=data)
        return IntentClassification(
            intent=IntentType.no_action,
            confidence=0.0,
            reasoning=f"parse error: {e!s}",
        )


__all__ = [
    "IntentClassifier",
    "_INTENT_TOOL",
    "_extract_tool_input",
    "_parse_classification",
    "classify_with_backend",
    "classify_with_llm",
]
