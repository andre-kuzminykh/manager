"""LLM backends for intent extraction.

Defines a thin interface so either Anthropic or OpenAI can drive the
structured-output call without the classifier caring about SDK differences.
"""
from __future__ import annotations

import json
from typing import Any, Protocol

from app.intent.prompts import SYSTEM_PROMPT
from app.logging_setup import get_logger

log = get_logger(__name__)


# Shared tool schema for intent extraction.
INTENT_TOOL_NAME = "record_intent"
INTENT_TOOL_DESCRIPTION = (
    "Record the extracted intent and structured draft for the Slack message."
)
INTENT_TOOL_PARAMETERS: dict[str, Any] = {
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
                "participants": {"type": "array", "items": {"type": "string"}},
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
}


class LLMBackend(Protocol):
    """Produces the tool_use input dict or None if the model refused."""

    def extract_intent(self, *, user_prompt: str) -> dict[str, Any] | None: ...

    def call_tool(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tool_name: str,
        tool_description: str,
        tool_parameters: dict[str, Any],
    ) -> dict[str, Any] | None: ...


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


class AnthropicBackend:
    def __init__(self, client: Any, model: str) -> None:
        self._client = client
        self._model = model

    def call_tool(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tool_name: str,
        tool_description: str,
        tool_parameters: dict[str, Any],
        model: str | None = None,
    ) -> dict[str, Any] | None:
        tool = {
            "name": tool_name,
            "description": tool_description,
            "input_schema": tool_parameters,
        }
        response = self._client.messages.create(
            model=model or self._model,
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[tool],
            tool_choice={"type": "tool", "name": tool_name},
            messages=[{"role": "user", "content": user_prompt}],
        )
        return _extract_anthropic_tool_input(response)

    def extract_intent(self, *, user_prompt: str) -> dict[str, Any] | None:
        return self.call_tool(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            tool_name=INTENT_TOOL_NAME,
            tool_description=INTENT_TOOL_DESCRIPTION,
            tool_parameters=INTENT_TOOL_PARAMETERS,
        )


def _extract_anthropic_tool_input(response: Any) -> dict[str, Any] | None:
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


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


class OpenAIBackend:
    """Structured output via OpenAI tool calls (chat.completions)."""

    def __init__(self, client: Any, model: str) -> None:
        self._client = client
        self._model = model

    def call_tool(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tool_name: str,
        tool_description: str,
        tool_parameters: dict[str, Any],
        model: str | None = None,
    ) -> dict[str, Any] | None:
        tool = {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": tool_description,
                "parameters": tool_parameters,
            },
        }
        response = self._client.chat.completions.create(
            model=model or self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            tools=[tool],
            tool_choice={
                "type": "function",
                "function": {"name": tool_name},
            },
            temperature=0,
        )
        return _extract_openai_tool_input(response)

    def extract_intent(self, *, user_prompt: str) -> dict[str, Any] | None:
        return self.call_tool(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            tool_name=INTENT_TOOL_NAME,
            tool_description=INTENT_TOOL_DESCRIPTION,
            tool_parameters=INTENT_TOOL_PARAMETERS,
        )

    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        temperature: float = 0.2,
    ) -> str:
        """FR-CR-05-39 — plain-text completion for the Fireflies
        summariser. Returns the model's text response, or empty
        string on failure."""
        try:
            resp = self._client.chat.completions.create(
                model=model or self._model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temperature,
            )
        except Exception:  # noqa: BLE001
            return ""
        try:
            return resp.choices[0].message.content or ""
        except (AttributeError, IndexError):
            return ""


def _extract_openai_tool_input(response: Any) -> dict[str, Any] | None:
    try:
        choice = response.choices[0]
    except (AttributeError, IndexError, TypeError):
        return None

    message = getattr(choice, "message", None)
    if message is None and isinstance(choice, dict):
        message = choice.get("message")

    tool_calls = (
        getattr(message, "tool_calls", None)
        if message is not None
        else None
    )
    if tool_calls is None and isinstance(message, dict):
        tool_calls = message.get("tool_calls")
    if not tool_calls:
        return None

    call = tool_calls[0]
    func = getattr(call, "function", None) or (
        call.get("function") if isinstance(call, dict) else None
    )
    arguments = (
        getattr(func, "arguments", None)
        if func is not None
        else None
    )
    if arguments is None and isinstance(func, dict):
        arguments = func.get("arguments")
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except json.JSONDecodeError:
            return None
    return None
