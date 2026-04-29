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

    # FR-CR-05-09 — same Supabase view that the historical migrator
    # reads from is also used by the live listener to pull the
    # adaptive context window (the last ~10k chars of chat). The bot
    # API itself doesn't ship history, so we lean on the colleague's
    # ingestion pipeline for it. Reader is optional — without
    # `TELEGRAM_SOURCE_DATABASE_URL` the ingest just runs without
    # adaptive context (same behaviour as before).
    reader = None
    if settings.telegram_source_database_url:
        try:
            from app.telegram_ingest import TelegramSourceReader

            reader = TelegramSourceReader(
                database_url=settings.telegram_source_database_url,
                view_name=settings.telegram_source_view,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("tg_listener_reader_setup_failed", error=str(e))

    ingest = TelegramIngestService(
        classifier=classifier, orchestrator=orchestrator, reader=reader
    )

    # FR-CR-05-28 — wire the Sheet-pull factories into the listener
    # so it polls operator edits from both Sheets every
    # ``SHEET_POLL_INTERVAL_SECONDS`` (default 60s). Edits land in
    # the DB on the next tick — no external cron needed.
    team_sheet_factory = None
    tasks_sheet_pull_factory = None
    try:
        from app.sync.factories import (
            build_sheets_pull_factory,
            build_team_sheet_factory,
        )

        team_sheet_factory = build_team_sheet_factory(settings)
        tasks_sheet_pull_factory = build_sheets_pull_factory(settings)
    except Exception as e:  # noqa: BLE001
        log.warning("tg_listener_sheet_factories_setup_failed", error=str(e))

    listener = TelegramListener(
        token=settings.telegram_bot_token,
        ingest=ingest,
        team_sheet_factory=team_sheet_factory,
        tasks_sheet_pull_factory=tasks_sheet_pull_factory,
        sheet_poll_interval_seconds=settings.sheet_poll_interval_seconds,
        # FR-CR-05-35 — listener-side polling of the Supabase TG
        # view. Off by default; flip via VIEW_REALTIME_ENABLED.
        view_realtime_enabled=settings.view_realtime_enabled,
        view_poll_interval_seconds=settings.view_poll_interval_seconds,
        view_poll_batch_size=settings.view_poll_batch_size,
    )

    # FR-CR-04-23 / FR-CR-04-26 — register the active TaskSyncer so
    # every persistence call from the Telegram process (initial
    # `create_task_from_draft` sync, button-driven `sync_task` calls
    # in the handlers) actually pushes the row to Google Sheets and
    # Tasks. Without this the syncer holder stays None and `sync_task`
    # silently no-ops — TG-created tasks were missing from the sheet.
    try:
        from app.sync.factories import (
            build_google_tasks_factory,
            build_sheets_factory,
        )
        from app.sync.task_sync import TaskSyncer, set_active_syncer

        set_active_syncer(
            TaskSyncer(
                sheets_factory=build_sheets_factory(settings),
                google_tasks_factory=build_google_tasks_factory(settings),
            )
        )
    except Exception as e:  # noqa: BLE001
        log.warning("tg_listener_syncer_setup_failed", error=str(e))

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
