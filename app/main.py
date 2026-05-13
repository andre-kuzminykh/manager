"""Entry point for the Slack task manager bot."""
from __future__ import annotations

import sys

from app.config import Settings, get_settings
from app.intent import IntentClassifier
from app.intent.llm_backends import AnthropicBackend, LLMBackend, OpenAIBackend
from app.logging_setup import get_logger, setup_logging
from app.orchestrator.finalize import FinalizeService
from app.slack_bot.app import build_app, run_socket_mode
from app.sync.factories import (
    build_google_tasks_factory,
    build_sheets_factory,
)

log = get_logger(__name__)


def _build_llm_backend(settings: Settings) -> LLMBackend | None:
    """Pick a backend based on LLM_PROVIDER + available keys.

    - "auto" (default): OpenAI if OPENAI_API_KEY set, else Anthropic if
      ANTHROPIC_API_KEY set, else None (rule-only).
    - "openai" / "anthropic": explicit selection.
    - "none": force rules only.
    """
    provider = settings.llm_provider
    if provider == "none":
        return None

    if provider in ("auto", "openai") and settings.openai_api_key:
        try:
            from openai import OpenAI
        except ImportError:
            log.warning("openai_sdk_not_installed")
        else:
            log.info("llm_backend_selected", provider="openai", model=settings.openai_model)
            return OpenAIBackend(OpenAI(api_key=settings.openai_api_key), settings.openai_model)

    if provider in ("auto", "anthropic") and settings.anthropic_api_key:
        try:
            from anthropic import Anthropic
        except ImportError:
            log.warning("anthropic_sdk_not_installed")
        else:
            log.info(
                "llm_backend_selected", provider="anthropic", model=settings.anthropic_model
            )
            return AnthropicBackend(
                Anthropic(api_key=settings.anthropic_api_key), settings.anthropic_model
            )

    if provider == "openai" and not settings.openai_api_key:
        log.warning("llm_provider_openai_selected_but_no_key")
    if provider == "anthropic" and not settings.anthropic_api_key:
        log.warning("llm_provider_anthropic_selected_but_no_key")
    log.info("llm_backend_selected", provider="none")
    return None


def run() -> None:
    setup_logging()
    settings = get_settings()

    if not settings.slack_bot_token:
        log.error("missing_slack_bot_token")
        sys.exit(2)
    if not settings.slack_app_token:
        log.error("missing_slack_app_token_for_socket_mode")
        sys.exit(2)

    backend = _build_llm_backend(settings)
    classifier = IntentClassifier(backend=backend)

    sheets_factory = build_sheets_factory(settings)
    gtasks_factory = build_google_tasks_factory(settings)

    finalizer = FinalizeService(
        settings=settings,
        sheets_service_factory=sheets_factory,
        google_tasks_service_factory=gtasks_factory,
    )

    # Register the central syncer so handlers can push status changes,
    # edits and deletes to Google Sheets / Tasks without threading the
    # factories through every signature.
    from app.sync.task_sync import TaskSyncer, set_active_syncer

    set_active_syncer(
        TaskSyncer(
            sheets_factory=sheets_factory,
            google_tasks_factory=gtasks_factory,
        )
    )

    app = build_app(settings=settings, classifier=classifier, finalizer=finalizer)

    # FR-CR-05-02 — register the cross-channel subscriber dispatcher
    # so every TransitionService.apply / apply_edit_reply fans out
    # DMs to non-owner subscribers regardless of which trigger
    # produced the change. The Slack process knows about both
    # channels: it has the bolt client (Slack DMs) and a freshly-
    # constructed TelegramSender (TG DMs over plain HTTP).
    try:
        from app.services.subscriber_updates import (
            SubscriberDispatcher,
            set_active_dispatcher,
        )
        from app.telegram_bot.sender import TelegramSender

        tg_sender = TelegramSender(token=settings.telegram_bot_token or "")
        set_active_dispatcher(
            SubscriberDispatcher(
                slack_poster=app.client,
                telegram_sender=tg_sender if tg_sender.enabled else None,
            )
        )
    except Exception as e:  # noqa: BLE001
        log.warning("subscriber_dispatcher_setup_failed", error=str(e))

    # One-shot workspace-wide employees sync. Best-effort: a Slack
    # outage here must not block startup.
    try:
        from app.db import session_scope
        from app.services import EmployeeDirectory

        directory = EmployeeDirectory(client=app.client, settings=settings)
        with session_scope() as session:
            touched = directory.sync_workspace_members(session)
        log.info("employees_startup_sync", touched=touched)
    except Exception as e:  # noqa: BLE001
        log.warning("employees_startup_sync_failed", error=str(e))

    # FR-CR-05-165 — agenda runner (daemon thread). No-op when
    # AGENDA_ENABLED=false; never blocks startup; failure here
    # mustn't take down the Slack bot.
    try:
        from app.agenda.runner import AgendaRunner
        from app.sync.factories import (
            build_calendar_credentials_factory,
            build_docs_factory,
        )

        agenda_runner = AgendaRunner(
            settings=settings,
            slack_client=app.client,
            llm_backend=backend,
            calendar_factory=build_calendar_credentials_factory(settings),
            docs_factory=build_docs_factory(settings),
        )
        agenda_runner.start()
    except Exception as e:  # noqa: BLE001
        log.warning("agenda_runner_startup_failed", error=str(e))

    log.info("starting_socket_mode")
    run_socket_mode(app, settings.slack_app_token)


if __name__ == "__main__":
    run()
