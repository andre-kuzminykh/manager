"""Periodic Telegram ingest CLI (FR-CR-04-26 / FR-CR-05-95).

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

Flags (FR-CR-05-95 — operator workflow «обнулил БД, надо
перепрочитать последние 50»):

  --limit N            Override the per-tick batch size for this
                       single run.
  --last N             Pull the N most recent messages from the
                       view IGNORING the watermark. Useful right
                       after `wipe_tasks` when the watermark
                       still says «processed up to msg X» but
                       the DB is empty. WARNING: each yielded
                       message is processed; if you didn't wipe
                       processed_telegram_messages first, the
                       drafts gate will skip rows already
                       bookmarked.

Exits 0 on success (even with per-message errors logged), 2 on
configuration problems.
"""
from __future__ import annotations

import argparse
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


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Override per-tick batch size for this run.",
    )
    p.add_argument(
        "--last",
        type=int,
        default=None,
        help=(
            "Re-process the N most recent messages from the view "
            "IGNORING the watermark. Use after `wipe_tasks` to "
            "reseed drafts."
        ),
    )
    return p.parse_args()


def main() -> int:
    setup_logging()
    args = _parse_args()
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
        classifier=classifier,
        orchestrator=orchestrator,
        # FR-CR-05-09 — feed the reader through so each classify
        # call gets the adaptive ~10k-char chat history window.
        reader=reader,
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

    if args.last is not None:
        # FR-CR-05-95 — pull last N from the view, ignoring the
        # watermark. `iter_newest` returns DESC by sent_at; we
        # reverse so prepare_drafts processes them in chronological
        # order (older first) — that way the per-message context
        # window is built from earlier messages, like the cron path.
        log.info(
            "telegram_ingest_starting",
            mode="last",
            last=args.last,
            view=settings.telegram_source_view,
        )
        page = list(reader.iter_newest(limit=args.last))
        page.reverse()
        # Drop the per-(chat_id, message_id) bookmark rows for
        # exactly these messages so `prepare_drafts` doesn't
        # short-circuit on its idempotency check. Other bookmarks
        # (untouched messages) stay intact so the next regular
        # cron tick still resumes from the watermark correctly.
        if page:
            from sqlalchemy import and_, or_

            with session_scope() as bm_session:
                preds = [
                    and_(
                        ProcessedTelegramMessage.chat_id == m.chat_id,
                        ProcessedTelegramMessage.message_id == m.message_id,
                    )
                    for m in page
                ]
                if preds:
                    bm_session.query(ProcessedTelegramMessage).filter(
                        or_(*preds)
                    ).delete(synchronize_session=False)
            log.info(
                "telegram_ingest_bookmark_cleared",
                cleared=len(page),
            )
    else:
        after_chat, after_msg = _resume_watermark()
        batch_size = args.limit or settings.telegram_ingest_batch_size
        log.info(
            "telegram_ingest_starting",
            watermark_chat=after_chat,
            watermark_msg=after_msg,
            view=settings.telegram_source_view,
            limit=batch_size,
        )
        page = list(
            reader.page(
                after_chat_id=after_chat,
                after_message_id=after_msg,
                limit=batch_size,
            )
        )
    if not page:
        log.info("telegram_ingest_no_new_messages")
        return 0

    # FR-CR-04-32 parity: when a TG bot token is configured, route
    # the freshly-ingested messages through the confirm-first widget
    # flow — drafts go to the author/admins as «Create this task?»
    # DMs and the user clicks ✅ Accept to materialise the Task.
    # When no token is set we fall back to the legacy immediate-
    # create path so the cron job still produces something useful.
    if settings.telegram_bot_token:
        from app.telegram_bot.cards import post_draft_confirmation
        from app.telegram_bot.sender import TelegramSender

        sender = TelegramSender(token=settings.telegram_bot_token)
        drafts_proposed = 0
        nothing = 0
        errors = 0
        for m in page:
            try:
                with session_scope() as session:
                    drafts = service.prepare_drafts(session, m)
                    if not drafts:
                        nothing += 1
                        continue
                    for d in drafts:
                        payload = d.payload or {}
                        try:
                            post_draft_confirmation(
                                sender=sender,
                                session=session,
                                draft=d,
                                source_chat_id=m.chat_id,
                                source_message_id=m.message_id,
                                author_user_id=str(m.user_id) if m.user_id else None,
                                owner_user_id=payload.get("owner_user_id"),
                            )
                        except Exception as e:  # noqa: BLE001
                            log.warning(
                                "telegram_ingest_widget_failed",
                                draft_id=d.id,
                                error=str(e),
                            )
                        drafts_proposed += 1
            except Exception as e:  # noqa: BLE001
                errors += 1
                log.warning(
                    "telegram_ingest_message_failed",
                    chat_id=m.chat_id,
                    message_id=m.message_id,
                    error=str(e),
                )
        log.info(
            "telegram_ingest_done",
            seen=len(page),
            drafts_proposed=drafts_proposed,
            no_action=nothing,
            errors=errors,
            mode="confirm_first",
        )
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
        mode="auto_confirm",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
