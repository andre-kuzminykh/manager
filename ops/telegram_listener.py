"""Telegram live listener daemon (FR-CR-04-27).

Runs forever, long-polling the Bot API for new messages and pushing
each through the same intent pipeline + persistence layer as the
Slack flow. Use this as a Docker container command or a systemd
service:

    docker run -d --name slack-task-tg-listener \\
        --network slack-task-net --env-file /root/slack-task/.env \\
        --restart unless-stopped \\
        slack-task-bot:local \\
        python -m ops.telegram_listener

Stops gracefully on SIGTERM (Ctrl-C / docker stop): the next save of
the listener offset is the last one, and `processed_telegram_messages`
backstops idempotency for any in-flight message that didn't get
written.

Pre-flight checklist:

- ``TELEGRAM_BOT_TOKEN`` is set in the env file.
- The bot has been added to the chats / groups it should listen to.
- For groups: BotFather → /mybots → bot → Bot Settings →
  Group Privacy → **Turn off**. Otherwise the bot only sees /commands
  and direct mentions.
"""
from __future__ import annotations

import sys

from app.config import get_settings
from app.intent import IntentClassifier
from app.logging_setup import get_logger, setup_logging
from app.orchestrator import Orchestrator
from app.telegram_bot.listener import TelegramListener
from app.telegram_ingest.service import TelegramIngestService
from ops.telegram_ingest import _build_llm_backend

log = get_logger(__name__)


def main() -> int:
    setup_logging()
    settings = get_settings()
    if not settings.telegram_bot_token:
        log.error("missing_telegram_bot_token")
        return 2

    backend = _build_llm_backend()
    classifier = IntentClassifier(backend=backend)
    orchestrator = Orchestrator(settings)
    ingest = TelegramIngestService(
        classifier=classifier, orchestrator=orchestrator
    )
    listener = TelegramListener(
        token=settings.telegram_bot_token,
        ingest=ingest,
    )
    try:
        listener.run_forever()
    except KeyboardInterrupt:
        log.info("telegram_listener_interrupted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
