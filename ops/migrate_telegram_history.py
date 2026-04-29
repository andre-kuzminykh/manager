"""One-shot historical Telegram ingest (FR-CR-04-26 / FR-CR-04-32).

Walks the entire Supabase view ``humanoid_tg_chats_readonly`` from
oldest to newest and runs every message through the intent
pipeline. By default — *confirm-first* — each detected task lands
as an ``ActionDraft`` in `proposed` state and the bot DMs every TG
admin (and the author when reachable) a «Create this task?» widget.
The Task itself is materialised only when the user clicks ✅ Accept.
This is the same gate the live listener uses for group messages
(FR-CR-04-32) — the historical backfill now matches it.

Use this once at deploy time to seed the team's existing Telegram
history. Run-of-the-mill incremental updates go through
``ops.telegram_ingest`` (cron-driven).

Usage::

    # Default — drafts go to your DM as widgets.
    python -m ops.migrate_telegram_history --since-days 1

    # Auto-confirm: legacy behaviour, every classified task is
    # written straight to the DB without a widget. Use only when
    # you really don't want to click N buttons.
    python -m ops.migrate_telegram_history --auto-confirm

    python -m ops.migrate_telegram_history --dry-run
    python -m ops.migrate_telegram_history --batch-size 500
    python -m ops.migrate_telegram_history --since 2026-04-28

Idempotent: each (chat_id, message_id) is recorded in
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
    p.add_argument(
        "--auto-confirm",
        action="store_true",
        help=(
            "Skip the confirm-first widget — write every classified "
            "task straight to the DB. Default behaviour is to create "
            "an ActionDraft and DM the author/admins a «Create this "
            "task?» widget instead (FR-CR-04-32 parity)."
        ),
    )
    p.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Log the source text + classifier verdict (intent, "
            "confidence, reasoning) for every message that reaches "
            "the pipeline. Useful when the run produces 0 drafts and "
            "you want to see WHY each candidate was rejected."
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


def _confirm_first_chunk(
    service,
    sender,
    messages: list,
    *,
    debug: bool = False,
) -> tuple[int, int, int]:
    """Per-message confirm-first processing for a chunk.

    For each message: classify into one or more `ActionDraft`s
    (state=proposed) and DM a «Create this task?» widget to the
    standard recipient set (author + admins). Returns
    ``(drafts_proposed, no_action_or_empty, errors)``.

    When ``debug=True`` every message is also re-classified upfront
    via the service's classifier (one extra detect-LLM call per
    candidate — bounded by the chunk size, not free) so we can log
    the source text + verdict + reasoning before taking the
    prepare_drafts path. Use this to figure out why a backfill is
    producing 0 drafts.
    """
    from app.telegram_bot.cards import post_draft_confirmation

    drafts_proposed = 0
    nothing = 0
    errors = 0
    for m in messages:
        try:
            if debug:
                # Repeat the classify call so we can log what the
                # LLM actually said. This is the same pipeline the
                # service uses internally; the result here is
                # discarded — `prepare_drafts` re-classifies.
                from app.context.retriever import ContextWindow
                from app.schemas.intent import InvocationType

                window = ContextWindow(
                    conversation_id=str(m.chat_id),
                    source_ts=str(m.message_id),
                    thread_ts=str(m.reply_to) if m.reply_to else None,
                    source_message={
                        "ts": str(m.message_id),
                        "user": (
                            str(m.user_id) if m.user_id else (m.user_name or "tg_unknown")
                        ),
                        "text": m.text,
                        "subtype": None,
                    },
                )
                classification = service._classifier.classify(  # noqa: SLF001
                    context=window,
                    invocation_type=InvocationType.passive,
                    known_employees=None,
                )
                log.info(
                    "telegram_history_debug",
                    chat_id=m.chat_id,
                    message_id=m.message_id,
                    text_preview=(m.text or "")[:200].replace("\n", " "),
                    intent=classification.intent.value,
                    confidence=round(classification.confidence, 2),
                    reasoning=(classification.reasoning or "")[:200],
                    tasks=[t.title[:80] for t in classification.tasks],
                )
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
                            "telegram_history_widget_failed",
                            draft_id=d.id,
                            error=str(e),
                        )
                    drafts_proposed += 1
        except Exception as e:  # noqa: BLE001
            errors += 1
            log.warning(
                "telegram_history_message_failed",
                chat_id=m.chat_id,
                message_id=m.message_id,
                error=str(e),
            )
    return drafts_proposed, nothing, errors


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

    confirm_first = not args.auto_confirm and not args.dry_run
    sender = None
    if confirm_first:
        from app.telegram_bot.sender import TelegramSender

        token = settings.telegram_bot_token or ""
        if not token:
            log.error(
                "telegram_history_confirm_first_needs_token",
                hint=(
                    "TELEGRAM_BOT_TOKEN is not set; cannot DM widgets. "
                    "Either set the token or pass --auto-confirm to "
                    "create tasks directly."
                ),
            )
            return 2
        sender = TelegramSender(token=token)

    overall = IngestReport()
    drafts_proposed_total = 0
    drafts_nothing_total = 0
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
            elif confirm_first:
                proposed, nothing, errs = _confirm_first_chunk(
                    service, sender, batch, debug=args.debug
                )
                drafts_proposed_total += proposed
                drafts_nothing_total += nothing
                overall.seen += len(batch)
                overall.errors += errs
                log.info(
                    "telegram_history_chunk_done",
                    seen=len(batch),
                    drafts_proposed=proposed,
                    nothing=nothing,
                    errors=errs,
                    mode="confirm_first",
                )
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
        elif confirm_first:
            proposed, nothing, errs = _confirm_first_chunk(
                service, sender, batch
            )
            drafts_proposed_total += proposed
            drafts_nothing_total += nothing
            overall.seen += len(batch)
            overall.errors += errs
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
        drafts_proposed=drafts_proposed_total,
        no_action=overall.no_action + drafts_nothing_total,
        skipped_already_processed=overall.skipped_already_processed,
        skipped_empty_text=overall.skipped_empty_text,
        skipped_too_old=skipped_too_old_total,
        errors=overall.errors,
        dry_run=args.dry_run,
        confirm_first=confirm_first,
        since=since_date.isoformat() if since_date else None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
