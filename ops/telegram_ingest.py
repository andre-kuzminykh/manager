"""Periodic Telegram ingest CLI (FR-CR-04-26).

Run from cron every few minutes:

    python -m ops.telegram_ingest

Reads new messages from the read-only Supabase view
(``humanoid_tg_chats_readonly``) — strictly after the highest
(chat_id, message_id) we've already recorded — runs each through
the existing intent pipeline, and persists confirmed tasks with
``source_kind = 'telegram'``.

Idempotent: each processed (chat_id, message_id) lands in
``processed_telegram_messages`` so re-running the cron is a no-op
on the same rows.

Exits 0 on success (even with per-message errors logged), 2 on
configuration problems.
"""
from __future__ import annotations

import sys

from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.intent import IntentClassifier
from app.intent.llm_backends import AnthropicBackend, LLMBackend, OpenAIBackend
from app.logging_setup import get_logger, setup_logging
from app.models import ProcessedTelegramMessage
from app.orchestrator import Orchestrator
from app.telegram_ingest import TelegramSourceReader
from app.telegram_ingest.service import TelegramIngestService

log = get_logger(__name__)


def _build_llm_backend() -> LLMBackend | None:
    """Same provider-resolution logic as `app/main.py:_build_llm_backend`,
    minus the bot wiring. Kept inline so this CLI doesn't import the
    Slack package."""
    settings = get_settings()
    if settings.llm_provider == "none":
        return None
    if settings.llm_provider in ("auto", "openai") and settings.openai_api_key:
        try:
            from openai import OpenAI

            return OpenAIBackend(
                OpenAI(api_key=settings.openai_api_key), settings.openai_model
            )
        except ImportError:
            pass
    if settings.llm_provider in ("auto", "anthropic") and settings.anthropic_api_key:
        try:
            from anthropic import Anthropic

            return AnthropicBackend(
                Anthropic(api_key=settings.anthropic_api_key), settings.anthropic_model
            )
        except ImportError:
            pass
    return None


def _resume_watermark() -> tuple[int | None, int | None]:
    """Highest (chat_id, message_id) we've already processed. ``None``
    on the very first run."""
    with session_scope() as session:
        row = (
            session.query(ProcessedTelegramMessage)
            .order_by(
                ProcessedTelegramMessage.chat_id.desc(),
                ProcessedTelegramMessage.message_id.desc(),
            )
            .first()
        )
        if row is None:
            return None, None
        return row.chat_id, row.message_id


def main() -> int:
    setup_logging()
    settings = get_settings()
    if not settings.telegram_source_database_url:
        log.error(
            "telegram_ingest_disabled",
            reason="TELEGRAM_SOURCE_DATABASE_URL is not set",
        )
        return 2

    reader = TelegramSourceReader(
        database_url=settings.telegram_source_database_url,
        view_name=settings.telegram_source_view,
    )

    backend = _build_llm_backend()
    classifier = IntentClassifier(backend=backend)
    orchestrator = Orchestrator(settings)
    service = TelegramIngestService(
        classifier=classifier, orchestrator=orchestrator
    )

    # FR-CR-04-23 / FR-CR-04-26 — register the active TaskSyncer so
    # `create_task_from_draft`'s initial-sync hook can push the new
    # row to Google Sheets. Without this the sync silently no-ops
    # and TG-ingested tasks were missing from the spreadsheet.
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
        log.warning("tg_ingest_syncer_setup_failed", error=str(e))

    after_chat, after_msg = _resume_watermark()
    log.info(
        "telegram_ingest_starting",
        watermark_chat=after_chat,
        watermark_msg=after_msg,
        view=settings.telegram_source_view,
    )

    page = list(
        reader.page(
            after_chat_id=after_chat,
            after_message_id=after_msg,
            limit=settings.telegram_ingest_batch_size,
        )
    )
    if not page:
        log.info("telegram_ingest_no_new_messages")
        return 0

    with session_scope() as session:
        report = service.process_batch(session, page)

    log.info(
        "telegram_ingest_done",
        seen=report.seen,
        tasks_created=report.tasks_created,
        no_action=report.no_action,
        skipped_already_processed=report.skipped_already_processed,
        skipped_empty_text=report.skipped_empty_text,
        errors=report.errors,
        error_samples=report.error_samples,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
