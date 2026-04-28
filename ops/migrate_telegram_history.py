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

Idempotent: each processed (chat_id, message_id) is recorded in
``processed_telegram_messages`` so a re-run picks up only new
messages added since.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import asdict

from app.config import get_settings
from app.db import session_scope
from app.intent import IntentClassifier
from app.logging_setup import get_logger, setup_logging
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
    return p.parse_args()


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


def main() -> int:
    setup_logging()
    args = _parse_args()
    settings = get_settings()
    if not settings.telegram_source_database_url:
        log.error("missing_telegram_source_database_url")
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

    overall = IngestReport()
    batch: list = []
    processed = 0
    for msg in reader.iter_all(batch_size=args.batch_size):
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

    log.info(
        "telegram_history_migration_done",
        seen=overall.seen,
        tasks_created=overall.tasks_created,
        no_action=overall.no_action,
        skipped_already_processed=overall.skipped_already_processed,
        skipped_empty_text=overall.skipped_empty_text,
        errors=overall.errors,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
