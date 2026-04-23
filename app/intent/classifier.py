from __future__ import annotations

import json
from datetime import date
from typing import Any

from app.config import get_settings
from app.context.retriever import ContextWindow
from app.intent.prompts import SYSTEM_PROMPT, build_user_prompt
from app.intent.rules import prefilter_intent
from app.logging_setup import get_logger
from app.schemas.intent import IntentClassification, IntentType, InvocationType

log = get_logger(__name__)


_INTENT_TOOL = {
    "name": "record_intent",
    "description": "Record the extracted intent and structured draft for the Slack message.",
    "input_schema": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": [
                    "create_task",
                    "create_meeting",
                    "update_task",
                    "update_meeting",
                    "no_action",
                ],
            },
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "reasoning": {"type": "string"},
            "task": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "owner_display_name": {"type": "string"},
                    "priority": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "urgent"],
                    },
                    "due_date": {
                        "type": "string",
                        "description": "ISO YYYY-MM-DD or null",
                    },
                },
                "required": ["title"],
            },
            "meeting": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "notes": {"type": "string"},
                    "participants": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "datetime_at": {
                        "type": "string",
                        "description": "ISO 8601 datetime with offset or null",
                    },
                    "timezone": {"type": "string"},
                },
                "required": ["title"],
            },
        },
        "required": ["intent", "confidence"],
    },
}


class IntentClassifier:
    """Thin wrapper that composes rule prefilter + LLM extraction."""

    def __init__(self, anthropic_client: Any | None = None) -> None:
        self._client = anthropic_client
        self._settings = get_settings()

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

        if self._client is None:
            # Without LLM we can only return the coarse hint. Mark confidence
            # proportional to how certain the prefilter was.
            return IntentClassification(
                intent=prefilter.hint,
                confidence=prefilter.score,
                reasoning="prefilter only (no LLM configured)",
            )

        return classify_with_llm(
            client=self._client,
            model=self._settings.anthropic_model,
            context=context,
            invocation_type=invocation_type,
            source_text=source_text,
        )


def classify_with_llm(
    *,
    client: Any,
    model: str,
    context: ContextWindow,
    invocation_type: InvocationType,
    source_text: str,
) -> IntentClassification:
    """Call the Anthropic Messages API with a tool-use schema for structured output."""

    user_prompt = build_user_prompt(
        source_text=source_text,
        context_messages=context.flat_messages(),
        invocation_type=invocation_type.value,
        current_date=date.today().isoformat(),
    )

    try:
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[_INTENT_TOOL],
            tool_choice={"type": "tool", "name": "record_intent"},
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as e:  # noqa: BLE001 -- we want to degrade gracefully
        log.error("intent_llm_call_failed", error=str(e))
        prefilter = prefilter_intent(source_text)
        return IntentClassification(
            intent=prefilter.hint,
            confidence=prefilter.score * 0.5,
            reasoning=f"LLM call failed, fell back to rules: {e!s}",
        )

    tool_input = _extract_tool_input(response)
    if tool_input is None:
        log.warning("intent_llm_no_tool_use", response=str(response))
        return IntentClassification(
            intent=IntentType.no_action,
            confidence=0.0,
            reasoning="LLM did not produce a tool_use block",
        )

    return _parse_classification(tool_input)


def _extract_tool_input(response: Any) -> dict[str, Any] | None:
    content = getattr(response, "content", None) or []
    for block in content:
        block_type = getattr(block, "type", None) or (
            block.get("type") if isinstance(block, dict) else None
        )
        if block_type == "tool_use":
            tool_input = getattr(block, "input", None)
            if tool_input is None and isinstance(block, dict):
                tool_input = block.get("input")
            if isinstance(tool_input, str):
                try:
                    return json.loads(tool_input)
                except json.JSONDecodeError:
                    return None
            if isinstance(tool_input, dict):
                return tool_input
    return None


def _parse_classification(data: dict[str, Any]) -> IntentClassification:
    # Pydantic will coerce strings into date/datetime where needed.
    try:
        return IntentClassification.model_validate(data)
    except Exception as e:  # noqa: BLE001
        log.warning("intent_parse_failed", error=str(e), data=data)
        return IntentClassification(
            intent=IntentType.no_action,
            confidence=0.0,
            reasoning=f"parse error: {e!s}",
        )
