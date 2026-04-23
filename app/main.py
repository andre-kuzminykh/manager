"""Entry point for the Slack task manager bot."""
from __future__ import annotations

import sys

from app.config import get_settings
from app.intent import IntentClassifier
from app.logging_setup import get_logger, setup_logging
from app.orchestrator.finalize import FinalizeService
from app.slack_bot.app import build_app, run_socket_mode
from app.sync.factories import (
    build_google_tasks_factory,
    build_sheets_factory,
)

log = get_logger(__name__)


def _build_anthropic_client(api_key: str):
    if not api_key:
        return None
    try:
        from anthropic import Anthropic
    except ImportError:  # pragma: no cover
        log.warning("anthropic_not_installed")
        return None
    return Anthropic(api_key=api_key)


def run() -> None:
    setup_logging()
    settings = get_settings()

    if not settings.slack_bot_token:
        log.error("missing_slack_bot_token")
        sys.exit(2)
    if not settings.slack_app_token:
        log.error("missing_slack_app_token_for_socket_mode")
        sys.exit(2)

    anthropic_client = _build_anthropic_client(settings.anthropic_api_key)
    classifier = IntentClassifier(anthropic_client=anthropic_client)

    sheets_factory = build_sheets_factory(settings)
    gtasks_factory = build_google_tasks_factory(settings)

    finalizer = FinalizeService(
        settings=settings,
        sheets_service_factory=sheets_factory,
        google_tasks_service_factory=gtasks_factory,
    )

    app = build_app(settings=settings, classifier=classifier, finalizer=finalizer)

    log.info("starting_socket_mode")
    run_socket_mode(app, settings.slack_app_token)


if __name__ == "__main__":
    run()
