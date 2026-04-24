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
from app.intent.date_resolver import resolve_due_date
from app.intent.owner_prompt import (
    OWNER_SYSTEM_PROMPT,
    OWNER_TOOL_DESCRIPTION,
    OWNER_TOOL_NAME,
    OWNER_TOOL_PARAMETERS,
    build_owner_user_prompt,
)
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
    ) -> IntentClassification:
        source_text = (context.source_message.get("text") or "").strip()
        prefilter = prefilter_intent(source_text)

        # Passive path: if rules say "no_action" with very low score AND not explicit,
        # return quickly without calling the LLM. This protects budget and keeps
        # passive chat quiet.
        if invocation_type == InvocationType.passive and prefilter.hint == IntentType.no_action:
            return IntentClassification(
                intent=IntentType.no_action,
                confidence=0.0,
                reasoning="prefilter: no task/meeting keywords",
            )

        if self._backend is None:
            # Without an LLM we can only return the coarse rule hint.
            return IntentClassification(
                intent=prefilter.hint,
                confidence=prefilter.score,
                reasoning="prefilter only (no LLM configured)",
            )

        return classify_with_backend(
            backend=self._backend,
            context=context,
            invocation_type=invocation_type,
            source_text=source_text,
        )


def classify_with_backend(
    *,
    backend: LLMBackend,
    context: ContextWindow,
    invocation_type: InvocationType,
    source_text: str,
) -> IntentClassification:
    user_prompt = build_user_prompt(
        source_text=source_text,
        context_messages=context.flat_messages(),
        invocation_type=invocation_type.value,
        current_date=date.today().isoformat(),
    )
    try:
        tool_input = backend.extract_intent(user_prompt=user_prompt)
    except Exception as e:  # noqa: BLE001 — degrade gracefully
        log.error("intent_llm_call_failed", error=str(e))
        prefilter = prefilter_intent(source_text)
        return IntentClassification(
            intent=prefilter.hint,
            confidence=prefilter.score * 0.5,
            reasoning=f"LLM call failed, fell back to rules: {e!s}",
        )

    if tool_input is None:
        log.warning("intent_llm_no_tool_use")
        return IntentClassification(
            intent=IntentType.no_action,
            confidence=0.0,
            reasoning="LLM did not produce a tool_use block",
        )

    classification = _parse_classification(tool_input)
    # Date: deterministic Python resolver owns this concern. If it finds a
    # clear phrase in source_text we overwrite the LLM's answer; otherwise
    # we keep the LLM's hint.
    if classification.task is not None:
        resolved = resolve_due_date(source_text, date.today())
        if resolved is not None:
            classification.task.due_date = resolved

    # Owner: small LLMs often pick the audience or the author as the
    # assignee when owner detection shares a prompt with intent + date.
    # A second focused LLM call with the same conversation context
    # produces much more reliable results on gpt-4o-mini.
    if classification.task is not None and classification.intent == IntentType.create_task:
        author_user_id = (context.source_message.get("user") or None)
        try:
            owner_data = backend.call_tool(
                system_prompt=OWNER_SYSTEM_PROMPT,
                user_prompt=build_owner_user_prompt(
                    source_text=source_text,
                    context_messages=context.flat_messages(),
                    author_user_id=author_user_id,
                ),
                tool_name=OWNER_TOOL_NAME,
                tool_description=OWNER_TOOL_DESCRIPTION,
                tool_parameters=OWNER_TOOL_PARAMETERS,
            )
        except Exception as e:  # noqa: BLE001 — degrade gracefully
            log.warning("owner_llm_call_failed", error=str(e))
            owner_data = None
        if isinstance(owner_data, dict):
            slack_user_id = (owner_data.get("slack_user_id") or None)
            display_name = (owner_data.get("display_name") or None)
            if slack_user_id:
                classification.task.owner_user_id = slack_user_id
                classification.task.owner_display_name = (
                    classification.task.owner_display_name or display_name
                )
            elif display_name:
                # Only fill display_name; owner_user_id stays null so the
                # downstream layer can try the employees directory.
                classification.task.owner_display_name = display_name
                classification.task.owner_user_id = None
            else:
                # The focused call said "no assignee" — trust it and
                # clear whatever the main pass may have guessed.
                classification.task.owner_display_name = None
                classification.task.owner_user_id = None
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
