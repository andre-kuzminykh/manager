"""FR-CR-05-39 — One-shot Fireflies meeting ingest CLI.

Pulls the most-recent N transcripts from the Fireflies API,
runs each through `FirefliesPipeline.process_one`, and reports
the counts.

Usage::

    # Test mode: last 5 recordings.
    python -m ops.migrate_fireflies --newest --limit 5

Idempotent: each `fireflies_id` lands in `meeting_recordings`
on first run; a re-run skips already-completed recordings on
the per-step bookmark flags.

Exits 0 on success (per-recording errors are logged but don't
abort the whole batch), 2 on configuration problems.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import asdict

from app.config import get_settings
from app.db import session_scope
from app.fireflies.client import FirefliesClient
from app.fireflies.pipeline import FirefliesPipeline
from app.logging_setup import get_logger, setup_logging
from ops.telegram_ingest import _build_llm_backend

log = get_logger(__name__)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Fireflies meeting ingest (one-shot).",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=5,
        help="How many newest recordings to pull (default 5).",
    )
    p.add_argument(
        "--newest",
        action="store_true",
        help=(
            "Required flag — pulls the `--limit` MOST-RECENT "
            "recordings (the only mode supported for now). The "
            "Fireflies GraphQL API doesn't support a stable "
            "watermark-based paginator, so we always pull from "
            "the top."
        ),
    )
    return p.parse_args()


def main() -> int:
    setup_logging()
    args = _parse_args()
    settings = get_settings()

    if not settings.fireflies_api_token:
        log.error(
            "fireflies_disabled_no_token",
            reason="FIREFLIES_API_TOKEN is not set",
        )
        return 2

    client = FirefliesClient(
        token=settings.fireflies_api_token,
        endpoint=settings.fireflies_api_url,
    )

    backend = _build_llm_backend()
    if backend is None:
        log.error(
            "fireflies_disabled_no_llm_backend",
            hint="Set OPENAI_API_KEY (or ANTHROPIC_API_KEY).",
        )
        return 2

    # Docs factory + TelegramSender for short-summary DMs.
    docs_factory = None
    sender = None
    try:
        from app.sync.factories import build_docs_factory

        docs_factory = build_docs_factory(settings)
    except Exception as e:  # noqa: BLE001
        log.warning("fireflies_docs_factory_setup_failed", error=str(e))
    if settings.telegram_bot_token:
        from app.telegram_bot.sender import TelegramSender

        sender = TelegramSender(token=settings.telegram_bot_token)

    pipeline = FirefliesPipeline(
        settings=settings,
        client=client,
        llm_backend=backend,
        docs_factory=docs_factory,
        sender=sender,
    )

    transcripts = client.list_transcripts(limit=args.limit)
    log.info(
        "fireflies_migration_starting",
        seen=len(transcripts),
        limit=args.limit,
    )

    processed = 0
    skipped = 0
    errors = 0
    tasks_total = 0
    for t in transcripts:
        try:
            with session_scope() as session:
                report = pipeline.process_one(session, t)
                if report.skipped_reason:
                    skipped += 1
                else:
                    processed += 1
                    tasks_total += report.tasks_created
                    log.info(
                        "fireflies_recording_processed",
                        fireflies_id=report.fireflies_id,
                        title=(report.title or "")[:80],
                        transcript_chars=report.transcript_chars,
                        detailed_chars=report.detailed_chars,
                        short_chars=report.short_chars,
                        google_doc_url=report.google_doc_url,
                        tasks_created=report.tasks_created,
                        short_dms=report.short_summary_recipients,
                        errors=report.errors,
                    )
        except Exception as e:  # noqa: BLE001
            errors += 1
            log.warning(
                "fireflies_recording_failed",
                fireflies_id=t.id,
                error=str(e),
            )

    log.info(
        "fireflies_migration_done",
        seen=len(transcripts),
        processed=processed,
        skipped=skipped,
        errors=errors,
        tasks_created=tasks_total,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
