"""FR-CR-05-90 — retroactively share existing meeting docs as
anyone-with-link writer.

New docs (post-FR-CR-05-59) are auto-shared on creation; this
CLI fixes EXISTING `meeting_recordings.google_doc_id` rows
that were created BEFORE the auto-share was deployed (or
where the share call silently failed under
`docs_share_anyone_with_link_failed`).

What it does:

  1. Lists every `MeetingRecording` with a non-null
     `google_doc_id`.
  2. Calls `permissions().create({"type":"anyone",
     "role":"writer"})` on each.
  3. Logs a per-doc success/failure line + a summary at the
     end.

Idempotent — `permissions.create({type:"anyone"})` doesn't
duplicate; the second call against an already-shared doc is
a no-op (200 response, same permission id).

Usage::

    sudo docker exec slack-task-bot python -m ops.retro_share_docs
    sudo docker exec slack-task-bot python -m ops.retro_share_docs --dry-run
    sudo docker exec slack-task-bot python -m ops.retro_share_docs --role reader   # read-only
"""
from __future__ import annotations

import argparse
import sys

from app.config import get_settings
from app.db import session_scope
from app.logging_setup import get_logger, setup_logging
from app.models import MeetingRecording
from app.sync.factories import build_docs_factory

log = get_logger(__name__)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="List doc ids without calling the Drive API.",
    )
    p.add_argument(
        "--role",
        default="writer",
        choices=("reader", "writer", "commenter"),
        help="Permission role for `type=anyone` (default: writer).",
    )
    return p.parse_args()


def main() -> int:
    setup_logging()
    args = _parse_args()
    settings = get_settings()

    factory = build_docs_factory(settings)
    if factory is None:
        log.error(
            "retro_share_docs_not_configured",
            hint=(
                "Set FIREFLIES_DOCS_FOLDER_ID + Drive credentials "
                "(GOOGLE_SHEETS_CREDENTIALS_JSON) so the SA can "
                "edit the doc permissions."
            ),
        )
        return 2

    docs = factory()
    if docs is None:
        log.error("retro_share_docs_no_credentials")
        return 2

    shared = 0
    skipped = 0
    failures = 0
    seen = 0

    with session_scope() as session:
        rows = (
            session.query(MeetingRecording)
            .filter(MeetingRecording.google_doc_id.isnot(None))
            .order_by(MeetingRecording.id.asc())
            .all()
        )
        for row in rows:
            seen += 1
            doc_id = row.google_doc_id
            if not doc_id:
                skipped += 1
                continue
            if args.dry_run:
                log.info(
                    "retro_share_docs_dry_run",
                    doc_id=doc_id,
                    fireflies_id=row.fireflies_id,
                    role=args.role,
                )
                continue
            try:
                docs._share_anyone_with_link(doc_id, role=args.role)
                shared += 1
                log.info(
                    "retro_share_docs_shared",
                    doc_id=doc_id,
                    fireflies_id=row.fireflies_id,
                    role=args.role,
                )
            except Exception as e:  # noqa: BLE001
                failures += 1
                log.warning(
                    "retro_share_docs_failed",
                    doc_id=doc_id,
                    fireflies_id=row.fireflies_id,
                    error=str(e),
                )

    log.info(
        "retro_share_docs_done",
        seen=seen,
        shared=shared,
        skipped=skipped,
        failures=failures,
        dry_run=args.dry_run,
        role=args.role,
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
