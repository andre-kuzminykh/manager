"""FR-CR-05-11 — Periodic Tasks-sheet ↔ DB sync.

Bidirectional sync for the Tasks spreadsheet. The DB → Sheet
direction runs continuously (event-driven via
`schedule_sync_task`); the Sheet → DB direction runs on this CLI,
typically on a 5-minute cron.

Operator workflow: edit a row directly in the spreadsheet (change
priority, push the due date, mark a task done), and the next
pull tick applies the edits to the DB. Status changes flow
through `TransitionService` so audit-log + subscribers fire as if
the change came from a button click.

Conflict rule: **the sheet wins**. If both sides edit the same
field within one tick interval, the sheet's value lands. No
clock-skew comparison.

Usage::

    python -m ops.pull_tasks_sheet

Exits 0 on success (per-row errors are logged, not raised), 2
when the sheet isn't configured.
"""
from __future__ import annotations

import sys

from app.config import get_settings
from app.db import session_scope
from app.logging_setup import get_logger, setup_logging
from app.sync.factories import build_sheets_pull_factory

log = get_logger(__name__)


def main() -> int:
    setup_logging()
    settings = get_settings()
    factory = build_sheets_pull_factory(settings)
    if factory is None:
        log.error(
            "tasks_sheet_pull_not_configured",
            hint=(
                "Set GOOGLE_SHEETS_SPREADSHEET_ID and ensure the "
                "service account has Editor access to the Tasks "
                "spreadsheet."
            ),
        )
        return 2

    sync = factory()
    if sync is None:
        log.error("tasks_sheet_pull_no_credentials")
        return 2

    with session_scope() as session:
        seen, changed, skipped = sync.pull(session)
    log.info(
        "tasks_sheet_pull_done",
        rows_seen=seen,
        rows_changed=changed,
        rows_skipped=skipped,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
