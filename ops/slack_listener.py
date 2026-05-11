"""FR-CR-05-162 — Slack ingest entrypoint.

Запускается отдельным контейнером (не объединённым с TG-listener):

    docker run -d --name slack-task-slack-ingest --restart unless-stopped \
      --network slack-task-net -e PYTHONPATH=/app \
      --env-file /home/admin_/tg-listener.env \
      slack-task-bot:latest python ops/slack_listener.py

Требует:
  - SLACK_INGEST_ENABLED=true
  - SLACK_BOT_TOKEN=xoxb-... (с history scopes + chat:write)
  - SLACK_APP_TOKEN=xapp-... (Socket Mode connection token,
    scope connections:write)
  - OPENAI_API_KEY (для LLM-classifier + drafts)
  - TELEGRAM_BOT_TOKEN (для TG-карточек)
"""
from __future__ import annotations

import sys

from app.config import get_settings
from app.intent import IntentClassifier
from app.intent.llm_backends import OpenAIBackend
from app.logging_setup import setup_logging, get_logger
from app.slack_ingest import run_socket_mode
from app.telegram_bot.sender import TelegramSender


def main() -> int:
    setup_logging()
    log = get_logger(__name__)
    settings = get_settings()

    if not getattr(settings, "slack_ingest_enabled", False):
        log.info(
            "slack_ingest_disabled_via_env",
            hint="set SLACK_INGEST_ENABLED=true to enable",
        )
        return 0

    if not (settings.slack_bot_token and settings.slack_app_token):
        log.error(
            "slack_ingest_missing_tokens",
            has_bot=bool(settings.slack_bot_token),
            has_app=bool(settings.slack_app_token),
        )
        return 2

    if not settings.openai_api_key:
        log.error("slack_ingest_no_openai_key")
        return 2

    # Build classifier (LLM-backed)
    from openai import OpenAI

    llm = OpenAIBackend(
        OpenAI(api_key=settings.openai_api_key),
        settings.openai_model,
    )
    classifier = IntentClassifier(backend=llm)

    # Build TG sender (for card delivery)
    tg_sender: TelegramSender | None = None
    if settings.telegram_bot_token:
        tg_sender = TelegramSender(token=settings.telegram_bot_token)
    else:
        log.warning(
            "slack_ingest_no_tg_token",
            hint="tasks will be persisted but no TG cards will be sent",
        )

    log.info(
        "slack_ingest_main_starting",
        bot_token_prefix=(settings.slack_bot_token or "")[:25] + "...",
        app_token_prefix=(settings.slack_app_token or "")[:25] + "...",
        tg_enabled=bool(tg_sender),
    )
    run_socket_mode(
        settings=settings,
        classifier=classifier,
        tg_sender=tg_sender,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
