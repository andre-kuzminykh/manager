"""Tests for the LLM backend abstraction (Anthropic + OpenAI parity)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.intent.llm_backends import (
    INTENT_TOOL_NAME,
    INTENT_TOOL_PARAMETERS,
    AnthropicBackend,
    OpenAIBackend,
    _extract_anthropic_tool_input,
    _extract_openai_tool_input,
)


# --------------------------------------------------------------------------- #
# Shared tool schema
# --------------------------------------------------------------------------- #


def test_shared_tool_schema_enum_matches_spec():
    enum_vals = set(INTENT_TOOL_PARAMETERS["properties"]["intent"]["enum"])
    assert enum_vals == {
        "create_task",
        "create_meeting",
        "update_task",
        "update_meeting",
        "no_action",
    }


def test_shared_tool_schema_requires_intent_and_confidence():
    assert set(INTENT_TOOL_PARAMETERS["required"]) == {"intent", "confidence"}


def test_shared_tool_name_is_stable():
    assert INTENT_TOOL_NAME == "record_intent"


# --------------------------------------------------------------------------- #
# Anthropic backend
# --------------------------------------------------------------------------- #


class _AnthropicStubClient:
    def __init__(self, tool_input=None, raise_exc=None):
        self.calls = []
        self._tool_input = tool_input
        self._raise = raise_exc

        class _Messages:
            def __init__(inner):
                inner._parent = self

            def create(inner, **kwargs):
                inner._parent.calls.append(kwargs)
                if inner._parent._raise is not None:
                    raise inner._parent._raise
                if inner._parent._tool_input is None:
                    return SimpleNamespace(content=[{"type": "text", "text": "no"}])
                return SimpleNamespace(
                    content=[
                        SimpleNamespace(
                            type="tool_use", input=inner._parent._tool_input
                        )
                    ]
                )

        self.messages = _Messages()


def test_anthropic_backend_extracts_tool_input():
    client = _AnthropicStubClient(
        tool_input={"intent": "create_task", "confidence": 0.9, "task": {"title": "x"}}
    )
    backend = AnthropicBackend(client, "claude-sonnet-4-6")
    out = backend.extract_intent(user_prompt="hello")
    assert out == {"intent": "create_task", "confidence": 0.9, "task": {"title": "x"}}


def test_anthropic_backend_passes_tool_choice():
    client = _AnthropicStubClient(tool_input={"intent": "no_action", "confidence": 0.0})
    AnthropicBackend(client, "claude-sonnet-4-6").extract_intent(user_prompt="x")
    kwargs = client.calls[0]
    assert kwargs["tool_choice"] == {"type": "tool", "name": INTENT_TOOL_NAME}
    assert kwargs["tools"][0]["name"] == INTENT_TOOL_NAME


def test_anthropic_backend_returns_none_without_tool_use():
    client = _AnthropicStubClient(tool_input=None)
    backend = AnthropicBackend(client, "claude-sonnet-4-6")
    assert backend.extract_intent(user_prompt="x") is None


def test_anthropic_backend_propagates_exception():
    client = _AnthropicStubClient(raise_exc=RuntimeError("boom"))
    backend = AnthropicBackend(client, "claude-sonnet-4-6")
    with pytest.raises(RuntimeError):
        backend.extract_intent(user_prompt="x")


def test_extract_anthropic_handles_dict_blocks():
    resp = SimpleNamespace(
        content=[{"type": "tool_use", "input": {"intent": "no_action", "confidence": 0.0}}]
    )
    assert _extract_anthropic_tool_input(resp) == {"intent": "no_action", "confidence": 0.0}


def test_extract_anthropic_handles_string_json_input():
    resp = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", input='{"intent":"no_action","confidence":0}')]
    )
    out = _extract_anthropic_tool_input(resp)
    assert out == {"intent": "no_action", "confidence": 0}


def test_extract_anthropic_empty_content_is_none():
    assert _extract_anthropic_tool_input(SimpleNamespace(content=[])) is None


# --------------------------------------------------------------------------- #
# OpenAI backend
# --------------------------------------------------------------------------- #


def _openai_response(arguments):
    func = SimpleNamespace(arguments=arguments, name=INTENT_TOOL_NAME)
    call = SimpleNamespace(function=func, id="call_1")
    message = SimpleNamespace(tool_calls=[call], content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _OpenAIStubClient:
    def __init__(self, response=None, raise_exc=None):
        self.calls = []
        self._response = response
        self._raise = raise_exc

        class _Completions:
            def __init__(inner):
                inner._parent = self

            def create(inner, **kwargs):
                inner._parent.calls.append(kwargs)
                if inner._parent._raise is not None:
                    raise inner._parent._raise
                return inner._parent._response

        class _Chat:
            def __init__(inner):
                inner.completions = _Completions()

        self.chat = _Chat()


def test_openai_backend_parses_tool_call_arguments():
    resp = _openai_response('{"intent":"create_task","confidence":0.9,"task":{"title":"x"}}')
    client = _OpenAIStubClient(response=resp)
    backend = OpenAIBackend(client, "gpt-4o-mini")
    out = backend.extract_intent(user_prompt="hello")
    assert out["intent"] == "create_task"
    assert out["confidence"] == 0.9
    assert out["task"]["title"] == "x"


def test_openai_backend_sends_function_tool():
    resp = _openai_response('{"intent":"no_action","confidence":0}')
    client = _OpenAIStubClient(response=resp)
    OpenAIBackend(client, "gpt-4o").extract_intent(user_prompt="x")
    kwargs = client.calls[0]
    assert kwargs["tools"][0]["type"] == "function"
    assert kwargs["tools"][0]["function"]["name"] == INTENT_TOOL_NAME
    assert kwargs["tool_choice"] == {
        "type": "function",
        "function": {"name": INTENT_TOOL_NAME},
    }
    assert kwargs["temperature"] == 0


def test_openai_backend_handles_dict_arguments():
    resp = _openai_response({"intent": "no_action", "confidence": 0})
    client = _OpenAIStubClient(response=resp)
    out = OpenAIBackend(client, "gpt-4o").extract_intent(user_prompt="x")
    assert out == {"intent": "no_action", "confidence": 0}


def test_openai_backend_returns_none_when_no_tool_calls():
    message = SimpleNamespace(tool_calls=None, content="sorry")
    resp = SimpleNamespace(choices=[SimpleNamespace(message=message)])
    client = _OpenAIStubClient(response=resp)
    out = OpenAIBackend(client, "gpt-4o").extract_intent(user_prompt="x")
    assert out is None


def test_openai_backend_returns_none_on_malformed_arguments():
    resp = _openai_response("not-json")
    client = _OpenAIStubClient(response=resp)
    out = OpenAIBackend(client, "gpt-4o").extract_intent(user_prompt="x")
    assert out is None


def test_openai_backend_propagates_exception():
    client = _OpenAIStubClient(raise_exc=RuntimeError("rate limit"))
    backend = OpenAIBackend(client, "gpt-4o")
    with pytest.raises(RuntimeError):
        backend.extract_intent(user_prompt="x")


def test_extract_openai_tool_input_from_dict_shape():
    resp = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": INTENT_TOOL_NAME,
                                "arguments": '{"intent":"no_action","confidence":0}',
                            }
                        }
                    ]
                }
            }
        ]
    }
    # Convert to object-ish so the extractor's .choices[0] works.
    obj = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    tool_calls=[
                        SimpleNamespace(
                            function=SimpleNamespace(
                                name=INTENT_TOOL_NAME,
                                arguments='{"intent":"no_action","confidence":0}',
                            )
                        )
                    ]
                )
            )
        ]
    )
    assert _extract_openai_tool_input(obj) == {"intent": "no_action", "confidence": 0}


# --------------------------------------------------------------------------- #
# Backend selection in main._build_llm_backend
# --------------------------------------------------------------------------- #


def _build_backend(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    from app.main import _build_llm_backend

    return _build_llm_backend(get_settings()), get_settings


def test_backend_selection_openai_preferred_when_key_set(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    from app.main import _build_llm_backend

    backend = _build_llm_backend(get_settings())
    assert isinstance(backend, OpenAIBackend)
    get_settings.cache_clear()  # type: ignore[attr-defined]


def test_backend_selection_anthropic_when_only_anthropic_key(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    from app.main import _build_llm_backend

    backend = _build_llm_backend(get_settings())
    assert isinstance(backend, AnthropicBackend)
    get_settings.cache_clear()  # type: ignore[attr-defined]


def test_backend_selection_none_when_no_keys(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    from app.main import _build_llm_backend

    assert _build_llm_backend(get_settings()) is None
    get_settings.cache_clear()  # type: ignore[attr-defined]


def test_backend_selection_forced_none(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    from app.main import _build_llm_backend

    assert _build_llm_backend(get_settings()) is None
    get_settings.cache_clear()  # type: ignore[attr-defined]


def test_backend_selection_forced_anthropic_ignores_openai(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    from app.main import _build_llm_backend

    backend = _build_llm_backend(get_settings())
    assert isinstance(backend, AnthropicBackend)
    get_settings.cache_clear()  # type: ignore[attr-defined]


def test_backend_selection_forced_openai_ignores_anthropic(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    from app.main import _build_llm_backend

    backend = _build_llm_backend(get_settings())
    assert isinstance(backend, OpenAIBackend)
    get_settings.cache_clear()  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def test_settings_default_provider_is_auto():
    assert Settings().llm_provider == "auto"


def test_settings_defaults_for_openai_model():
    # FR-CR-05-104 — promoted from gpt-4o-mini → gpt-5.5
    # (operator choice; rolled back via OPENAI_MODEL env var
    # if the key doesn't have access).
    assert Settings().openai_model == "gpt-5.5"


def test_model_uses_completion_tokens_helper():
    """FR-CR-05-106 — gpt-5.x / o1 / o3 / o4 reject
    `max_tokens`; everything else still wants it."""
    from app.intent.llm_backends import _model_uses_completion_tokens

    assert _model_uses_completion_tokens("gpt-5.5") is True
    assert _model_uses_completion_tokens("gpt-5.5-mini") is True
    assert _model_uses_completion_tokens("o1-preview") is True
    assert _model_uses_completion_tokens("o3-mini") is True
    assert _model_uses_completion_tokens("o4") is True
    assert _model_uses_completion_tokens("gpt-4o") is False
    assert _model_uses_completion_tokens("gpt-4o-mini") is False
    assert _model_uses_completion_tokens("gpt-4-turbo") is False
    assert _model_uses_completion_tokens("gpt-3.5-turbo") is False
    assert _model_uses_completion_tokens(None) is False
    assert _model_uses_completion_tokens("") is False


def test_openai_call_uses_completion_tokens_for_gpt5(monkeypatch):
    """FR-CR-05-106 — operator regression: gpt-5.5 returned
    400 «'max_tokens' is not supported with this model. Use
    'max_completion_tokens' instead.». Backend now picks the
    right kwarg per-model."""
    from app.intent.llm_backends import OpenAIBackend

    captured: dict = {}

    class _StubChoices:
        message = type("M", (), {"content": "{}", "tool_calls": None})()

    class _StubResp:
        choices = [_StubChoices()]

    class _StubCompletions:
        def create(self, **kw):
            captured.update(kw)
            return _StubResp()

    class _StubChat:
        completions = _StubCompletions()

    class _StubClient:
        chat = _StubChat()

    backend = OpenAIBackend(_StubClient(), "gpt-5.5")
    backend.call_tool(
        system_prompt="s",
        user_prompt="u",
        tool_name="t",
        tool_description="d",
        tool_parameters={"type": "object"},
    )
    assert "max_completion_tokens" in captured
    assert "max_tokens" not in captured
    assert captured["max_completion_tokens"] == 4096
    # FR-CR-05-107 — gpt-5.x rejects custom temperature; we
    # omit the kwarg entirely (server uses default 1).
    assert "temperature" not in captured


def test_openai_call_passes_reasoning_effort_for_gpt5(monkeypatch):
    """FR-CR-05-120 — operator pinned `reasoning.effort=high`
    for task extraction so gpt-5.5 spends more think-budget on
    each call. Only forwarded for gpt-5.x / o-series; the
    4o-family rejects the kwarg, so the backend gates it on the
    same model classifier as max_completion_tokens."""
    from app.intent.llm_backends import OpenAIBackend

    captured: dict = {}

    class _StubChoices:
        message = type("M", (), {"content": "{}", "tool_calls": None})()

    class _StubResp:
        choices = [_StubChoices()]

    class _StubCompletions:
        def create(self, **kw):
            captured.update(kw)
            return _StubResp()

    class _StubChat:
        completions = _StubCompletions()

    class _StubClient:
        chat = _StubChat()

    backend = OpenAIBackend(_StubClient(), "gpt-5.5")
    backend.call_tool(
        system_prompt="s",
        user_prompt="u",
        tool_name="t",
        tool_description="d",
        tool_parameters={"type": "object"},
        reasoning_effort="high",
    )
    assert captured.get("reasoning_effort") == "high"

    # No effort → kwarg absent (server-side default kicks in).
    captured.clear()
    backend.call_tool(
        system_prompt="s",
        user_prompt="u",
        tool_name="t",
        tool_description="d",
        tool_parameters={"type": "object"},
    )
    assert "reasoning_effort" not in captured


def test_openai_call_retries_without_reasoning_effort_on_400(monkeypatch):
    """FR-CR-05-120 follow-up — operator regression: gpt-5.5 +
    function tools + reasoning_effort returns 400 in
    /v1/chat/completions («Function tools with reasoning_effort
    are not supported … Please use /v1/responses instead.»).
    Backend retries once without the kwarg so the pipeline
    still gets tasks out, just without the tunable think
    budget. A future Responses API rewrite would re-enable it."""
    from app.intent.llm_backends import OpenAIBackend

    captured_calls: list[dict] = []

    class _StubChoices:
        message = type("M", (), {"content": "{}", "tool_calls": None})()

    class _StubResp:
        choices = [_StubChoices()]

    class _StubCompletions:
        def create(self, **kw):
            captured_calls.append(dict(kw))
            if "reasoning_effort" in kw:
                raise RuntimeError(
                    "Error code: 400 - {'error': {'message': 'Function "
                    "tools with reasoning_effort are not supported for "
                    "gpt-5.5 in /v1/chat/completions. Please use "
                    "/v1/responses instead.', 'type': "
                    "'invalid_request_error', 'param': "
                    "'reasoning_effort'}}"
                )
            return _StubResp()

    class _StubChat:
        completions = _StubCompletions()

    class _StubClient:
        chat = _StubChat()

    backend = OpenAIBackend(_StubClient(), "gpt-5.5")
    backend.call_tool(
        system_prompt="s",
        user_prompt="u",
        tool_name="t",
        tool_description="d",
        tool_parameters={"type": "object"},
        reasoning_effort="high",
    )
    # First attempt with reasoning_effort, second retry without.
    assert len(captured_calls) == 2
    assert captured_calls[0].get("reasoning_effort") == "high"
    assert "reasoning_effort" not in captured_calls[1]


def test_openai_call_drops_reasoning_effort_for_gpt4o(monkeypatch):
    """FR-CR-05-120 — gpt-4o-family rejects `reasoning_effort`
    (only reasoning models accept it). Same gate as
    max_completion_tokens."""
    from app.intent.llm_backends import OpenAIBackend

    captured: dict = {}

    class _StubChoices:
        message = type("M", (), {"content": "{}", "tool_calls": None})()

    class _StubResp:
        choices = [_StubChoices()]

    class _StubCompletions:
        def create(self, **kw):
            captured.update(kw)
            return _StubResp()

    class _StubChat:
        completions = _StubCompletions()

    class _StubClient:
        chat = _StubChat()

    backend = OpenAIBackend(_StubClient(), "gpt-4o")
    backend.call_tool(
        system_prompt="s",
        user_prompt="u",
        tool_name="t",
        tool_description="d",
        tool_parameters={"type": "object"},
        reasoning_effort="high",  # operator-set, but gpt-4o ignores
    )
    assert "reasoning_effort" not in captured


def test_settings_default_fireflies_tasks_reasoning_effort_high():
    """FR-CR-05-120 — operator default. Pipelines call
    `call_tool(..., reasoning_effort=settings.fireflies_tasks_reasoning_effort)`,
    so this knob controls the per-call think budget for both
    Fireflies and Zoom task extraction."""
    from app.config import Settings

    s = Settings()
    assert s.fireflies_tasks_reasoning_effort == "high"


def test_openai_call_uses_max_tokens_for_gpt4o(monkeypatch):
    """FR-CR-05-106 — gpt-4o still wants `max_tokens` (legacy
    name); the helper differentiates."""
    from app.intent.llm_backends import OpenAIBackend

    captured: dict = {}

    class _StubChoices:
        message = type("M", (), {"content": "{}", "tool_calls": None})()

    class _StubResp:
        choices = [_StubChoices()]

    class _StubCompletions:
        def create(self, **kw):
            captured.update(kw)
            return _StubResp()

    class _StubChat:
        completions = _StubCompletions()

    class _StubClient:
        chat = _StubChat()

    backend = OpenAIBackend(_StubClient(), "gpt-4o")
    backend.call_tool(
        system_prompt="s",
        user_prompt="u",
        tool_name="t",
        tool_description="d",
        tool_parameters={"type": "object"},
    )
    assert "max_tokens" in captured
    assert "max_completion_tokens" not in captured
    # gpt-4o still accepts temperature=0 for deterministic output.
    assert captured.get("temperature") == 0


def test_openai_complete_text_drops_temperature_for_gpt5(monkeypatch):
    """FR-CR-05-107 — Fireflies summariser uses `complete_text`.
    gpt-5.x rejects custom temperature; the kwarg must be
    omitted for those models."""
    from app.intent.llm_backends import OpenAIBackend

    captured: dict = {}

    class _StubChoices:
        message = type("M", (), {"content": "summary text"})()

    class _StubResp:
        choices = [_StubChoices()]

    class _StubCompletions:
        def create(self, **kw):
            captured.update(kw)
            return _StubResp()

    class _StubChat:
        completions = _StubCompletions()

    class _StubClient:
        chat = _StubChat()

    backend = OpenAIBackend(_StubClient(), "gpt-5.5")
    out = backend.complete_text(
        system_prompt="s", user_prompt="u", temperature=0.2,
    )
    assert out == "summary text"
    assert "temperature" not in captured


def test_openai_complete_text_keeps_temperature_for_gpt4o():
    """FR-CR-05-107 — gpt-4o-family still accepts temperature."""
    from app.intent.llm_backends import OpenAIBackend

    captured: dict = {}

    class _StubChoices:
        message = type("M", (), {"content": "x"})()

    class _StubResp:
        choices = [_StubChoices()]

    class _StubCompletions:
        def create(self, **kw):
            captured.update(kw)
            return _StubResp()

    class _StubChat:
        completions = _StubCompletions()

    class _StubClient:
        chat = _StubChat()

    backend = OpenAIBackend(_StubClient(), "gpt-4o-mini")
    backend.complete_text(
        system_prompt="s", user_prompt="u", temperature=0.2,
    )
    assert captured.get("temperature") == 0.2


def test_settings_openai_api_key_field_is_empty_by_default():
    assert Settings(OPENAI_API_KEY="").openai_api_key == ""


def test_settings_accepts_custom_openai_model():
    s = Settings(OPENAI_MODEL="gpt-4o")
    assert s.openai_model == "gpt-4o"
