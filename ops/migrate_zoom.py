"""FR-CR-05-116 — One-shot Zoom recording ingest CLI.

Pulls the most-recent N cloud recordings from the Zoom API,
runs each through `ZoomPipeline.process_one`, and reports
the counts.

Usage::

    python -m ops.migrate_zoom --newest --limit 5

Idempotent: each `zoom_id` (Zoom UUID) lands in
`zoom_recordings` on first run; a re-run skips already-
completed recordings on the per-step bookmark flags.

Exits 0 on success (per-recording errors are logged but
don't abort the whole batch), 2 on configuration problems.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import asdict

from app.config import get_settings
from app.db import session_scope
from app.logging_setup import get_logger, setup_logging
from app.sync.factories import build_docs_factory
from app.zoom.client import ZoomClient
from app.zoom.pipeline import ZoomPipeline
from ops.telegram_ingest import _build_llm_backend

log = get_logger(__name__)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Zoom Cloud Recording ingest.")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument(
        "--newest", action="store_true",
        help=(
            "Pulls the `--limit` most-recent recordings. Required "
            "unless `--zoom-id` is given (which targets a single "
            "recording by its UUID)."
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
        "--zoom-id",
        default=None,
        help=(
            "FR-CR-05-143 — process exactly ONE recording by its "
            "Zoom UUID. Useful for targeted reruns on a known "
            "meeting (`HdyK6m9iQtKabZFpT6bN1Q==`) without touching "
            "neighbouring recordings. Pages the recording listing "
            "until the UUID is found. Implies a single-row run."
        ),
    )
    p.add_argument(
        "--page-size",
        type=int,
        default=30,
        help=(
            "FR-CR-05-143 — Zoom API page size for the listing "
            "request. Bump up to 300 when host filter is on and "
            "the target host's recordings are sparse."
        ),
    )
    p.add_argument(
        "--days-back",
        type=int,
        default=30,
        help=(
            "FR-CR-05-143 — when `--zoom-id` is given, search "
            "this many days back. Zoom API defaults to LAST 24 "
            "HOURS when from/to are unspecified, so historical "
            "UUIDs need a wider window. Default 30 days."
        ),
    )
    return p.parse_args()


def main() -> int:
    setup_logging()
    args = _parse_args()
    settings = get_settings()
    if not (
        settings.zoom_account_id
        and settings.zoom_client_id
        and settings.zoom_client_secret
    ):
        log.error(
            "zoom_migration_disabled",
            reason="ZOOM_ACCOUNT_ID / ZOOM_CLIENT_ID / "
                   "ZOOM_CLIENT_SECRET not all set",
        )
        return 2
    if not args.newest and not args.zoom_id:
        log.error(
            "zoom_migration_requires_newest_or_zoom_id",
            hint="pass either `--newest` or `--zoom-id <uuid>`",
        )
        return 2

    client = ZoomClient(
        account_id=settings.zoom_account_id,
        client_id=settings.zoom_client_id,
        client_secret=settings.zoom_client_secret,
        api_base=settings.zoom_api_base,
        oauth_url=settings.zoom_oauth_url,
    )
    if not client.enabled:
        log.error("zoom_client_disabled")
        return 2

    llm = _build_llm_backend()
    docs_factory = build_docs_factory(settings)

    sender = None
    if settings.telegram_bot_token:
        from app.telegram_bot.sender import TelegramSender

        sender = TelegramSender(token=settings.telegram_bot_token)

    pipeline = ZoomPipeline(
        settings=settings,
        client=client,
        llm_backend=llm,
        docs_factory=docs_factory,
        sender=sender,
    )

    # FR-CR-05-143 — when `--zoom-id` is given we keep paging
    # until the target uuid surfaces (or we run out of pages).
    # When `--newest`, we just take the first --limit recordings,
    # filtered by ZOOM_REQUIRED_EMAIL (host OR participant) if set.
    required_email = (settings.zoom_required_email or "").strip() or None
    if args.zoom_id:
        # Need to find ONE specific recording by UUID — required-
        # email filter is bypassed (operator picked the uuid).
        # Zoom API defaults to last 24 hours when from/to are
        # absent, so for an arbitrary historical UUID we widen
        # the window to `--days-back` (default 30).
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz

        now = _dt.now(_tz.utc).date()
        from_date = (now - _td(days=args.days_back)).isoformat()
        to_date = now.isoformat()
        all_metas = client.list_recordings(
            limit=args.page_size, page_size=args.page_size,
            from_date=from_date, to_date=to_date,
        )
        metas = [m for m in all_metas if m.id == args.zoom_id]
        if not metas:
            log.error(
                "zoom_migration_zoom_id_not_found",
                zoom_id=args.zoom_id,
                page_size=args.page_size,
                from_date=from_date, to_date=to_date,
                page_total=len(all_metas),
                hint=(
                    "uuid not in window — bump --days-back / "
                    "--page-size (max 300) or check the uuid"
                ),
            )
            return 2
    else:
        metas = client.list_recordings(
            limit=args.limit,
            page_size=args.page_size,
            required_email=required_email,
        )
    log.info(
        "zoom_migration_starting",
        limit=args.limit,
        zoom_id=args.zoom_id,
        required_email=required_email,
        seen=len(metas),
        rerun=args.rerun,
    )

    # FR-CR-05-117 — symmetric with `migrate_fireflies --rerun`:
    # reset every step flag + step output on the matched rows so
    # the pipeline re-executes from scratch. Tasks already
    # extracted from those recordings are NOT deleted.
    if args.rerun and metas:
        from app.models import ZoomRecording

        ids = [m.id for m in metas]
        with session_scope() as session:
            rows = (
                session.query(ZoomRecording)
                .filter(ZoomRecording.zoom_id.in_(ids))
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
                r.attempts = 0
            log.info(
                "zoom_rerun_reset",
                count=len(rows),
                ids=[r.zoom_id for r in rows],
            )

    processed = 0
    errors = 0
    tasks_created = 0
    for m in metas:
        with session_scope() as session:
            try:
                report = pipeline.process_one(session, m)
                processed += 1
                tasks_created += report.tasks_created
                if report.errors:
                    errors += 1
                log.info("zoom_recording_processed", **asdict(report))
            except Exception as e:  # noqa: BLE001
                errors += 1
                log.warning(
                    "zoom_recording_failed",
                    zoom_id=m.id, error=str(e),
                )
    log.info(
        "zoom_migration_done",
        seen=len(metas),
        processed=processed,
        errors=errors,
        tasks_created=tasks_created,
        skipped=0,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
