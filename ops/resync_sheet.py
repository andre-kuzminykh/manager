"""FR-CR-05-89 — Bulk re-sync every Task to the Google Sheet.

Operator workflow: «давай я все задачи дропнул в шит,
перезальем туда». Use this when:

  - the operator manually cleared / damaged the spreadsheet,
  - the FR-CR-05-86 column drift left rows in the wrong place,
  - title cap rules changed (FR-CR-05-72 / -89) and existing
    rows have stale long titles you want renormalised.

What it does:

  1. Loads every non-deleted ``Task`` row.
  2. Re-applies ``normalize_task_title`` (FR-CR-05-72/75/89)
     in-place — so old rows with 200-char titles become
     ≤100-char one-glance labels.
  3. Resets ``Task.google_sheets_row_id`` and the matching
     ``GoogleSheetsSync.row_id`` to ``None`` so the next sync
     is a fresh ``append`` into A:V (per FR-CR-05-86) instead
     of an in-place ``update`` against a row that may no
     longer exist.
  4. Iterates ``sheets.sync(session, task)`` for each.

Idempotent — running twice is a no-op past the first pass
(the rows already point at correct row_ids after the first
run).

Usage::

    python -m ops.resync_sheet              # all open tasks
    python -m ops.resync_sheet --dry-run    # log what WOULD happen
    python -m ops.resync_sheet --include-deleted  # also push
                                # tombstoned rows so the
                                # «status=deleted» row lands
"""
from __future__ import annotations

import argparse
import sys

from app.config import get_settings
from app.db import session_scope
from app.logging_setup import get_logger, setup_logging
from app.models import GoogleSheetsSync, Task
from app.persistence.tasks import normalize_task_title
from app.sync.factories import build_sheets_factory

log = get_logger(__name__)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would happen without touching the sheet.",
    )
    p.add_argument(
        "--include-deleted",
        action="store_true",
        help="Also re-push soft-deleted tasks (status=deleted rows).",
    )
    return p.parse_args()


def main() -> int:
    setup_logging()
    args = _parse_args()
    settings = get_settings()

    factory = build_sheets_factory(settings)
    if factory is None:
        log.error(
            "resync_sheet_not_configured",
            hint="Set GOOGLE_SHEETS_SPREADSHEET_ID and credentials.",
        )
        return 2

    sheets = factory()
    if sheets is None:
        log.error("resync_sheet_no_credentials")
        return 2

    titles_capped = 0
    rows_reset = 0
    rows_synced = 0
    failures = 0

    with session_scope() as session:
        q = session.query(Task)
        if not args.include_deleted:
            q = q.filter(Task.deleted_at.is_(None))
        tasks = q.order_by(Task.id.asc()).all()
        log.info("resync_sheet_start", task_count=len(tasks), dry_run=args.dry_run)

        for task in tasks:
            new_title = normalize_task_title(task.title)
            if new_title != task.title:
                titles_capped += 1
                if not args.dry_run:
                    task.title = new_title
            # Clear the legacy row pointer so append lands the row
            # fresh in A:V (FR-CR-05-86) — this also recovers from
            # operator-deleted rows.
            if task.google_sheets_row_id is not None:
                rows_reset += 1
                if not args.dry_run:
                    task.google_sheets_row_id = None
            sync_row = (
                session.query(GoogleSheetsSync)
                .filter_by(task_id=task.id)
                .one_or_none()
            )
            if sync_row is not None and sync_row.row_id is not None:
                if not args.dry_run:
                    sync_row.row_id = None

        if not args.dry_run:
            session.flush()
            for task in tasks:
                try:
                    sheets.sync(session, task)
                    rows_synced += 1
                except Exception as e:  # noqa: BLE001
                    failures += 1
                    log.warning(
                        "resync_sheet_row_failed",
                        task_id=task.id,
                        error=str(e),
                    )
        # session_scope commits on exit

    log.info(
        "resync_sheet_done",
        titles_capped=titles_capped,
        rows_reset=rows_reset,
        rows_synced=rows_synced,
        failures=failures,
        dry_run=args.dry_run,
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
