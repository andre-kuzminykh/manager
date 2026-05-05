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
    p.add_argument(
        "--rerun",
        action="store_true",
        help=(
            "FR-CR-05-117 — re-run pipeline on the latest "
            "recordings even if they're already processed. "
            "Resets all step flags + last_error + "
            "tasks_extracted_count + transcript / summary / "
            "doc text on existing rows so each step runs again. "
            "Use after prompt updates or to refresh stale "
            "summaries. Existing Tasks extracted from those "
            "recordings are NOT deleted (operator's responsibility "
            "via wipe_tasks if needed)."
        ),
    )
    p.add_argument(
        "--transcript-id",
        default=None,
        help=(
            "FR-CR-05-153 — process exactly ONE Fireflies "
            "transcript by id (e.g. `01KQFEVKGBBNR0ZQMKBJTE4EP3`). "
            "Pulls `--limit` recent transcripts and filters to "
            "the matching one. Bump `--limit` if the target is "
            "older than the default 5-recording window."
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
    # FR-CR-05-153 — `--transcript-id` narrows to one specific
    # recording. If not in the first `--limit` window, operator
    # bumps `--limit`.
    if args.transcript_id:
        target_id = args.transcript_id.strip()
        all_count = len(transcripts)
        transcripts = [t for t in transcripts if t.id == target_id]
        if not transcripts:
            log.error(
                "fireflies_transcript_id_not_found",
                transcript_id=target_id,
                page_total=all_count,
                hint=(
                    "id not in the first --limit window. "
                    "Bump --limit (e.g. 50) and retry."
                ),
            )
            return 2
    log.info(
        "fireflies_migration_starting",
        seen=len(transcripts),
        limit=args.limit,
        rerun=args.rerun,
        transcript_id=args.transcript_id,
    )

    # FR-CR-05-117 — `--rerun` resets the step flags + step
    # outputs on already-processed recordings so the pipeline
    # re-executes every step. Used after prompt updates to
    # refresh existing summaries.
    if args.rerun and transcripts:
        from app.models import MeetingRecording

        ids = [t.id for t in transcripts]
        with session_scope() as session:
            rows = (
                session.query(MeetingRecording)
                .filter(MeetingRecording.fireflies_id.in_(ids))
                .all()
            )
            for r in rows:
                r.audio_downloaded = False
                r.transcribed = False
                r.detailed_summarised = False
                r.short_summary_sent = False
                r.doc_exported = False
                r.tasks_extracted = False
                r.transcript_text = None
                r.detailed_summary = None
                r.short_summary = None
                r.google_doc_id = None
                r.google_doc_url = None
                r.tasks_extracted_count = None
                r.last_error = None
                r.processed_at = None
            log.info(
                "fireflies_rerun_reset",
                count=len(rows),
                ids=[r.fireflies_id for r in rows],
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
