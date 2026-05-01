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

    # FR-CR-05-39 — Fireflies pipeline wired in. Disabled unless
    # FIREFLIES_API_TOKEN is set; the toggle on top of that
    # decides whether the listener also polls automatically.
    if settings.fireflies_api_token:
        try:
            from app.fireflies.client import FirefliesClient
            from app.fireflies.pipeline import FirefliesPipeline
            from app.sync.factories import build_docs_factory

            ff_client = FirefliesClient(
                token=settings.fireflies_api_token,
                endpoint=settings.fireflies_api_url,
            )
            ff_pipeline = FirefliesPipeline(
                settings=settings,
                client=ff_client,
                llm_backend=backend,
                docs_factory=build_docs_factory(settings),
                sender=listener._sender,  # noqa: SLF001 — same process
            )
            listener.wire_fireflies(
                pipeline=ff_pipeline,
                enabled=settings.fireflies_realtime_enabled,
                poll_interval_seconds=settings.fireflies_poll_interval_seconds,
                poll_batch_size=settings.fireflies_poll_batch_size,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("tg_listener_fireflies_setup_failed", error=str(e))

    # FR-CR-05-118 — same scaffolding for Zoom Cloud Recordings.
    # Disabled unless ZOOM_ACCOUNT_ID/CLIENT_ID/SECRET trio is
    # set; ZOOM_REALTIME_ENABLED then decides whether the
    # listener polls in real time.
    if (
        settings.zoom_account_id
        and settings.zoom_client_id
        and settings.zoom_client_secret
    ):
        try:
            from app.sync.factories import build_docs_factory
            from app.zoom.client import ZoomClient
            from app.zoom.pipeline import ZoomPipeline

            zm_client = ZoomClient(
                account_id=settings.zoom_account_id,
                client_id=settings.zoom_client_id,
                client_secret=settings.zoom_client_secret,
                api_base=settings.zoom_api_base,
                oauth_url=settings.zoom_oauth_url,
            )
            zm_pipeline = ZoomPipeline(
                settings=settings,
                client=zm_client,
                llm_backend=backend,
                docs_factory=build_docs_factory(settings),
                sender=listener._sender,  # noqa: SLF001 — same process
            )
            listener.wire_zoom(
                pipeline=zm_pipeline,
                enabled=settings.zoom_realtime_enabled,
                poll_interval_seconds=settings.zoom_poll_interval_seconds,
                poll_batch_size=settings.zoom_poll_batch_size,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("tg_listener_zoom_setup_failed", error=str(e))

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

    # FR-CR-05-61 — wire the Google Tasks pull-side service so the
    # listener picks up edits / deletes the operator made directly
    # in Google Tasks UI and propagates them back to DB + the live
    # TG card. Off when `GOOGLE_TASKS_DEFAULT_TASKLIST_ID` is empty.
    try:
        from app.sync.factories import build_google_tasks_pull_factory

        gt_pull_factory = build_google_tasks_pull_factory(settings)
        if gt_pull_factory is not None:
            listener.wire_google_tasks_pull(
                factory=gt_pull_factory,
                poll_interval_seconds=settings.google_tasks_pull_interval_seconds,
            )
    except Exception as e:  # noqa: BLE001
        log.warning("tg_listener_google_tasks_pull_setup_failed", error=str(e))

    # FR-CR-05-124 — counterparties directory wipe-and-reload from
    # the configured Google Sheets. Off when neither
    # COUNTERPARTIES_STATUS_SHEET_ID nor _OUTREACH_SHEET_ID is set.
    try:
        from app.sync.factories import build_counterparties_sheet_factory

        cp_factory = build_counterparties_sheet_factory(settings)
        if cp_factory is not None:
            listener.wire_counterparties_pull(
                factory=cp_factory,
                poll_interval_seconds=(
                    settings.counterparties_poll_interval_seconds
                ),
            )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "tg_listener_counterparties_setup_failed", error=str(e),
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
