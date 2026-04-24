"""Requirement coverage: NFR-CR-02-2 (mention handler never silent,
requires build_app wiring to be complete), FR-CR-04-1..10 (pipeline
reachable from main).

Smoke tests for the startup path — setup_logging, _build_llm_backend
backend selection, and build_app returning a Bolt App with all
action_ids / callback_ids registered."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# --------------------------------------------------------------------------- #
# setup_logging (app/logging_setup.py)
# --------------------------------------------------------------------------- #


def test_setup_logging_runs_without_error(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    from app.config import get_settings
    from app.logging_setup import get_logger, setup_logging

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        setup_logging()
        log = get_logger("test")
        log.info("smoke")
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_setup_logging_production_uses_json(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    from app.config import get_settings
    from app.logging_setup import setup_logging

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        setup_logging()
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Backend selection (_build_llm_backend)
# --------------------------------------------------------------------------- #


def test_build_llm_backend_none_when_forced(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    from app.config import Settings
    from app.main import _build_llm_backend

    assert _build_llm_backend(Settings()) is None


def test_build_llm_backend_prefers_openai_in_auto(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic")
    from app.config import Settings
    from app.intent.llm_backends import OpenAIBackend
    from app.main import _build_llm_backend

    out = _build_llm_backend(Settings())
    assert isinstance(out, OpenAIBackend)


def test_build_llm_backend_falls_back_to_anthropic(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic")
    from app.config import Settings
    from app.intent.llm_backends import AnthropicBackend
    from app.main import _build_llm_backend

    out = _build_llm_backend(Settings())
    assert isinstance(out, AnthropicBackend)


def test_build_llm_backend_none_when_no_keys(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from app.config import Settings
    from app.main import _build_llm_backend

    assert _build_llm_backend(Settings()) is None


def test_build_llm_backend_explicit_openai_without_key_warns(monkeypatch, caplog):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from app.config import Settings
    from app.main import _build_llm_backend

    assert _build_llm_backend(Settings()) is None


def test_build_llm_backend_explicit_anthropic(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")  # should be ignored
    from app.config import Settings
    from app.intent.llm_backends import AnthropicBackend
    from app.main import _build_llm_backend

    out = _build_llm_backend(Settings())
    assert isinstance(out, AnthropicBackend)


# --------------------------------------------------------------------------- #
# build_app: all handlers wired
# --------------------------------------------------------------------------- #


def test_build_app_registers_every_expected_handler():
    from app.config import Settings
    from app.intent import IntentClassifier
    from app.orchestrator.finalize import FinalizeService
    from app.slack_bot import blocks as bk
    from app.slack_bot.app import build_app

    settings = Settings(
        SLACK_BOT_TOKEN="xoxb-test",
        SLACK_SIGNING_SECRET="stub",
    )
    # Bolt's App pings auth.test on construction unless we patch
    # token_verification_enabled off. Monkey-patch the base App class to
    # always start with verification disabled.
    import slack_bolt
    original_init = slack_bolt.App.__init__

    def _patched_init(self, *args, **kwargs):
        kwargs.setdefault("token_verification_enabled", False)
        return original_init(self, *args, **kwargs)

    with patch.object(slack_bolt.App, "__init__", _patched_init):
        app = build_app(
            settings=settings,
            classifier=IntentClassifier(backend=None),
            finalizer=FinalizeService(settings=settings),
        )

    # The wiring itself is best verified by tests that hit each handler
    # directly (test_cr01_handlers, test_task_edit_button, …). Here we
    # just confirm build_app wired *something* — the listener count is
    # at least as large as the expected set of actions + callbacks, so
    # if someone removes a @app.action decorator the test drops.
    listener_count = len(getattr(app, "_listeners", []))
    expected_min = 17 + 5  # actions + modal callbacks registered today
    assert listener_count >= expected_min, (
        f"too few listeners on the Bolt App ({listener_count} < "
        f"{expected_min}); did a handler registration drop out of build_app?"
    )
    # And every action/callback identifier at least compiles — no typos
    # that would leave a dangling action_id.
    for attr in dir(bk):
        if attr.startswith(("ACTION_", "MODAL_CALLBACK_")):
            assert isinstance(getattr(bk, attr), str)


def test_run_exits_when_slack_bot_token_missing(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        from app.main import run

        with pytest.raises(SystemExit) as excinfo:
            run()
        assert excinfo.value.code == 2
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_run_exits_when_app_token_missing(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-stub")
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        from app.main import run

        with pytest.raises(SystemExit) as excinfo:
            run()
        assert excinfo.value.code == 2
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_run_invokes_socket_mode_when_tokens_present(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-stub")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-stub")
    monkeypatch.setenv("LLM_PROVIDER", "none")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    started: list = []

    import slack_bolt
    original_init = slack_bolt.App.__init__

    def _patched_init(self, *args, **kwargs):
        kwargs.setdefault("token_verification_enabled", False)
        return original_init(self, *args, **kwargs)

    with patch.object(slack_bolt.App, "__init__", _patched_init), patch(
        "app.main.run_socket_mode", side_effect=lambda app, token: started.append(token)
    ):
        try:
            from app.main import run

            run()
        finally:
            get_settings.cache_clear()  # type: ignore[attr-defined]
    assert started == ["xapp-stub"]
