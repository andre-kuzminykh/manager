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
    assert Settings().openai_model == "gpt-4o-mini"


def test_settings_openai_api_key_field_is_empty_by_default():
    assert Settings(OPENAI_API_KEY="").openai_api_key == ""


def test_settings_accepts_custom_openai_model():
    s = Settings(OPENAI_MODEL="gpt-4o")
    assert s.openai_model == "gpt-4o"
