"""One-shot historical Telegram ingest (FR-CR-04-26).

Walks the entire Supabase view ``humanoid_tg_chats_readonly`` from
oldest to newest, processes every message through the intent pipeline
and persists tasks with ``source_kind = 'telegram'``.

Use this once at deploy time to seed the team's existing Telegram
history. Run-of-the-mill incremental updates go through
``ops.telegram_ingest`` (cron-driven).

Usage::

    python -m ops.migrate_telegram_history --dry-run
    python -m ops.migrate_telegram_history
    python -m ops.migrate_telegram_history --batch-size 500
    python -m ops.migrate_telegram_history --since 2026-04-28
    python -m ops.migrate_telegram_history --since-days 1

Idempotent: each processed (chat_id, message_id) is recorded in
``processed_telegram_messages`` so a re-run picks up only new
messages added since. Messages older than the optional ``--since``
cutoff are bookmarked as «skipped (too old)» without consuming any
LLM budget — re-runs of the same script don't re-process them
either.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone

from app.config import get_settings
from app.db import session_scope
from app.intent import IntentClassifier
from app.logging_setup import get_logger, setup_logging
from app.models import ProcessedTelegramMessage
from app.orchestrator import Orchestrator
from app.telegram_ingest import TelegramSourceReader
from app.telegram_ingest.service import IngestReport, TelegramIngestService
from ops.telegram_ingest import _build_llm_backend

log = get_logger(__name__)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Historical Telegram ingest (one-shot).",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=200,
        help="Page size for reading the view (default 200).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Read messages but don't write anything to the local DB.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Stop after processing this many messages (0 = no limit).",
    )
    p.add_argument(
        "--since",
        type=str,
        default=None,
        help=(
            "Only process messages with sent_at >= this UTC date "
            "(YYYY-MM-DD). Older messages are bookmarked as «skipped» "
            "so they don't waste LLM budget — and a re-run is fast."
        ),
    )
    p.add_argument(
        "--since-days",
        type=int,
        default=None,
        help=(
            "Shortcut: --since (today - N days). Mutually exclusive "
            "with --since."
        ),
    )
    args = p.parse_args()
    if args.since and args.since_days is not None:
        p.error("Pass either --since or --since-days, not both.")
    return args


def _resolve_since(args: argparse.Namespace) -> date | None:
    if args.since:
        try:
            return date.fromisoformat(args.since)
        except ValueError:
            raise SystemExit(
                f"--since must be YYYY-MM-DD, got {args.since!r}"
            )
    if args.since_days is not None:
        return date.today() - timedelta(days=args.since_days)
    return None


def _merge(a: IngestReport, b: IngestReport) -> IngestReport:
    return IngestReport(
        seen=a.seen + b.seen,
        skipped_already_processed=a.skipped_already_processed
        + b.skipped_already_processed,
        skipped_empty_text=a.skipped_empty_text + b.skipped_empty_text,
        no_action=a.no_action + b.no_action,
        tasks_created=a.tasks_created + b.tasks_created,
        errors=a.errors + b.errors,
        error_samples=(a.error_samples + b.error_samples)[:5],
    )


def _bookmark_skipped(messages: list) -> int:
    """Bookmark messages we're skipping due to ``--since`` so a
    re-run doesn't pull them through the reader again. Returns the
    count actually written (existing rows are skipped). Idempotent."""
    if not messages:
        return 0
    written = 0
    with session_scope() as session:
        for m in messages:
            existing = session.get(
                ProcessedTelegramMessage, (m.chat_id, m.message_id)
            )
            if existing is not None:
                continue
            session.add(
                ProcessedTelegramMessage(
                    chat_id=m.chat_id,
                    message_id=m.message_id,
                    processed_at=datetime.now(timezone.utc),
                    task_id=None,
                )
            )
            written += 1
    return written


def main() -> int:
    setup_logging()
    args = _parse_args()
    settings = get_settings()
    if not settings.telegram_source_database_url:
        log.error("missing_telegram_source_database_url")
        return 2

    since_date: date | None = _resolve_since(args)
    if since_date:
        log.info("telegram_history_since_filter", since=since_date.isoformat())

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

    # Same syncer wiring as `ops.telegram_listener` / `ops.telegram_
    # ingest`: register the active TaskSyncer so the historical
    # backfill also lands rows in Google Sheets, not just the DB.
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
        log.warning("tg_history_syncer_setup_failed", error=str(e))

    overall = IngestReport()
    batch: list = []
    skipped_too_old: list = []
    skipped_too_old_total = 0
    processed = 0
    for msg in reader.iter_all(batch_size=args.batch_size):
        # Date cutoff: messages older than `since_date` get a
        # «skipped» bookmark and never touch the LLM. We keep them
        # off the main batch so the LLM budget goes only to the
        # interesting range.
        if since_date and msg.sent_at and msg.sent_at.date() < since_date:
            skipped_too_old.append(msg)
            if len(skipped_too_old) >= args.batch_size:
                if not args.dry_run:
                    skipped_too_old_total += _bookmark_skipped(skipped_too_old)
                else:
                    skipped_too_old_total += len(skipped_too_old)
                skipped_too_old = []
            continue

        batch.append(msg)
        if len(batch) >= args.batch_size:
            if args.dry_run:
                log.info(
                    "telegram_history_dry_run_chunk",
                    seen=len(batch),
                    head=str(batch[0].message_id) if batch else None,
                )
                overall.seen += len(batch)
            else:
                with session_scope() as session:
                    report = service.process_batch(session, batch)
                overall = _merge(overall, report)
                log.info(
                    "telegram_history_chunk_done",
                    **{k: v for k, v in asdict(report).items() if k != "error_samples"},
                )
            processed += len(batch)
            batch = []
            if args.limit and processed >= args.limit:
                break
    if batch and not args.limit or (batch and processed < args.limit):
        if args.dry_run:
            overall.seen += len(batch)
        else:
            with session_scope() as session:
                report = service.process_batch(session, batch)
            overall = _merge(overall, report)
    if skipped_too_old:
        if not args.dry_run:
            skipped_too_old_total += _bookmark_skipped(skipped_too_old)
        else:
            skipped_too_old_total += len(skipped_too_old)

    log.info(
        "telegram_history_migration_done",
        seen=overall.seen,
        tasks_created=overall.tasks_created,
        no_action=overall.no_action,
        skipped_already_processed=overall.skipped_already_processed,
        skipped_empty_text=overall.skipped_empty_text,
        skipped_too_old=skipped_too_old_total,
        errors=overall.errors,
        dry_run=args.dry_run,
        since=since_date.isoformat() if since_date else None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
