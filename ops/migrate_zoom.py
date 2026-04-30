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
        help="Required flag — pulls the `--limit` most-recent recordings.",
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
    if not args.newest:
        log.error("zoom_migration_requires_newest_flag")
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

    metas = client.list_recordings(limit=args.limit)
    log.info("zoom_migration_starting", limit=args.limit, seen=len(metas))

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
