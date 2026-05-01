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
        # FR-CR-05-46 — multi-task extraction. A single message
        # can carry several distinct tasks («надо разработать
        # бота, а ещё дашборд» = 2 tasks). The canonical shape is
        # `tasks` (array); the legacy `task` (singular) is kept
        # for back-compat — both are accepted by the parser, but
        # for any message with TWO+ actionable items the LLM MUST
        # emit `tasks` so each gets its own card.
        "tasks": {
            "type": "array",
            "description": (
                "Every separately-actionable task in the message. "
                "Use this when the source carries more than one "
                "distinct action (split on conjunctions like «а "
                "ещё», «и»; on enumerations «во-первых, во-вторых»; "
                "on multiple verbs each describing a different "
                "task). One task → still allowed to use this with "
                "a single-item array. NEVER concatenate two "
                "actions into one title."
            ),
            "items": {
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
        },
        "task": {
            "type": "object",
            "description": (
                "Legacy single-task field — kept for back-compat. "
                "Prefer `tasks` (array) for any new extraction. "
                "If both are emitted, `tasks` wins."
            ),
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
        reasoning_effort: str | None = None,  # noqa: ARG002 — OpenAI-only kwarg, accepted for parity
    ) -> dict[str, Any] | None:
        tool = {
            "name": tool_name,
            "description": tool_description,
            "input_schema": tool_parameters,
        }
        response = self._client.messages.create(
            model=model or self._model,
            max_tokens=4096,  # FR-CR-05-70 — fits multi-task array + 3-6 sentence descriptions without mid-sentence cuts
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


def _model_uses_completion_tokens(model: str | None) -> bool:
    """FR-CR-05-106 — newer OpenAI models (`gpt-5*`, `o1*`,
    `o3*`, `o4*`) reject the `max_tokens` parameter and require
    `max_completion_tokens` instead. Old models (`gpt-4o`,
    `gpt-4-turbo`, `gpt-3.5-turbo`) still want `max_tokens`.
    """
    if not model:
        return False
    m = model.lower()
    return (
        m.startswith("gpt-5")
        or m.startswith("o1")
        or m.startswith("o3")
        or m.startswith("o4")
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
        reasoning_effort: str | None = None,
    ) -> dict[str, Any] | None:
        tool = {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": tool_description,
                "parameters": tool_parameters,
            },
        }
        # FR-CR-05-70 — bump max output to 4096 so multi-task
        # arrays + 3-6 sentence descriptions land complete.
        # FR-CR-05-106 — gpt-5.5 (and other reasoning-style
        # models) reject `max_tokens` and demand
        # `max_completion_tokens`. Probe the request shape and
        # retry on the «unsupported_parameter» error.
        eff_model = model or self._model
        kwargs: dict[str, Any] = dict(
            model=eff_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            tools=[tool],
            tool_choice={
                "type": "function",
                "function": {"name": tool_name},
            },
        )
        # FR-CR-05-106 / -107 — gpt-5.x and o-series reasoning
        # models reject `max_tokens` (must be `max_completion_
        # tokens`) AND `temperature=0` (only default 1
        # supported). Older gpt-4o-family accepts both. Branch
        # on the model name + retry-on-error as a safety net.
        if _model_uses_completion_tokens(eff_model):
            kwargs["max_completion_tokens"] = 4096
            # No temperature → defaults to 1 server-side.
            # FR-CR-05-120 — pass `reasoning_effort` (low /
            # medium / high) so a thinking-aware caller can ask
            # for deeper reasoning on hard prompts. Only
            # supported on gpt-5.x / o-series; 4o-family
            # rejects the kwarg, so we gate on the same
            # reasoning-style model classifier above.
            if reasoning_effort:
                kwargs["reasoning_effort"] = reasoning_effort
        else:
            kwargs["max_tokens"] = 4096
            kwargs["temperature"] = 0

        def _try(call_kwargs: dict[str, Any]):
            return self._client.chat.completions.create(**call_kwargs)

        try:
            response = _try(kwargs)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            # max_tokens / max_completion_tokens swap.
            if (
                "max_tokens" in msg
                and "max_completion_tokens" in msg
                and "max_tokens" in kwargs
            ):
                kwargs.pop("max_tokens", None)
                kwargs["max_completion_tokens"] = 4096
                kwargs.pop("temperature", None)
                response = _try(kwargs)
            elif (
                "max_completion_tokens" in msg
                and "Unsupported" in msg
                and "max_completion_tokens" in kwargs
            ):
                kwargs.pop("max_completion_tokens", None)
                kwargs["max_tokens"] = 4096
                kwargs.setdefault("temperature", 0)
                response = _try(kwargs)
            elif (
                "temperature" in msg
                and ("Unsupported" in msg or "unsupported" in msg)
                and "temperature" in kwargs
            ):
                # Reasoning model rejecting temperature=0.
                kwargs.pop("temperature", None)
                response = _try(kwargs)
            else:
                raise
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
        """FR-CR-05-39 / -107 — plain-text completion for the
        Fireflies summariser. Returns the model's text response,
        or empty string on failure. Reasoning models (gpt-5.x /
        o-series) reject custom temperature; we drop the kwarg
        for them."""
        eff_model = model or self._model
        kwargs: dict[str, Any] = dict(
            model=eff_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        if not _model_uses_completion_tokens(eff_model):
            kwargs["temperature"] = temperature
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except Exception as e:  # noqa: BLE001
            # Reasoning model rejected temperature; retry without.
            if (
                "temperature" in str(e)
                and "temperature" in kwargs
            ):
                kwargs.pop("temperature", None)
                try:
                    resp = self._client.chat.completions.create(**kwargs)
                except Exception:  # noqa: BLE001
                    return ""
            else:
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
