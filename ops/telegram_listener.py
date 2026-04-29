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

    # FR-CR-05-02 — register the cross-channel subscriber dispatcher
    # so transitions / edits triggered from Telegram buttons fan out
    # DMs to both Slack subscribers (via a fresh WebClient — only
    # outbound chat_postMessage, no socket mode needed) and Telegram
    # subscribers (via the listener's existing TelegramSender).
    try:
        from app.services.subscriber_updates import (
            SubscriberDispatcher,
            set_active_dispatcher,
        )

        slack_poster = None
        if settings.slack_bot_token:
            try:
                from slack_sdk import WebClient

                slack_poster = WebClient(token=settings.slack_bot_token)
            except Exception as e:  # noqa: BLE001
                log.warning("slack_webclient_setup_failed", error=str(e))
        set_active_dispatcher(
            SubscriberDispatcher(
                slack_poster=slack_poster,
                telegram_sender=listener._sender,  # noqa: SLF001 — same process
            )
        )
    except Exception as e:  # noqa: BLE001
        log.warning("subscriber_dispatcher_setup_failed", error=str(e))

    try:
        listener.run_forever()
    except KeyboardInterrupt:
        log.info("telegram_listener_interrupted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
