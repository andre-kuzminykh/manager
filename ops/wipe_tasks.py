"""FR-CR-05-91 — DESTRUCTIVE wipe of all task data.

Operator workflow: «давай обнулим все данные по задачам и
начнем вести их заново». Wipes:

  - tasks
  - action_drafts
  - task_status_history
  - task_subscriptions
  - google_sheets_sync (so resync starts fresh)
  - google_tasks_sync
  - daily_plan_items
  - audit_logs (categories that gate digests / weekly /
    daily plans, NOT the team-management ones)

KEEPS (so re-ingestion picks up where it left off):

  - team_members (registry — operator-edited)
  - telegram_chat_members (has_started_bot flags)
  - processed_telegram_messages (replay-protection bookmark
    so old TG messages don't re-create tasks)
  - meeting_recordings (Fireflies bookmark — won't re-process
    already-handled meetings)

Uses SQLAlchemy `.delete()` so it works against the live
Postgres deploy AND the SQLite test DB. Two-step confirm:
prints the row counts that WILL be deleted, then waits for
`--yes` flag before committing.

Usage::

    sudo docker exec slack-task-bot python -m ops.wipe_tasks --dry-run
    sudo docker exec slack-task-bot python -m ops.wipe_tasks --yes
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import delete, or_

from app.db import session_scope
from app.logging_setup import get_logger, setup_logging
from app.models import (
    ActionDraft,
    AuditLog,
    GoogleSheetsSync,
    GoogleTasksSync,
    Task,
    TaskStatusHistory,
    TaskSubscription,
)

log = get_logger(__name__)


# audit_logs categories swept on wipe — these are «task lifecycle»
# rows, NOT team-management bookmarks. Anything not listed stays
# (e.g. team_sheet_sync bookmarks).
_WIPED_AUDIT_CATEGORIES = (
    "telegram_morning_cards",
    "telegram_evening_status",
    "telegram_admin_watchlist",
    "telegram_morning_digest",
    "telegram_evening_plan",
    "telegram_morning_plan",
    "telegram_weekly_plan",
    "telegram_deadlines",
    "telegram_thread_reminders",
    "telegram_starts_now",
    "daily_plan",
    "task_status",
    "task_subscription",
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the row counts that would be deleted, do nothing.",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Required to actually run the wipe. Without it, --dry-run is forced.",
    )
    p.add_argument(
        "--include-daily-plan-items",
        action="store_true",
        default=True,
        help="(default) also wipe daily_plan_items.",
    )
    p.add_argument(
        "--also-wipe-sheet",
        action="store_true",
        help=(
            "Also clear every row past the header in the Google Sheet "
            "(FR-CR-05-94 — operator: «почему предыдущие задачи есть в "
            "google sheet, я думал все снести»). Header row is preserved."
        ),
    )
    return p.parse_args()


def _wipe_sheet_data(*, log_) -> int:
    """FR-CR-05-94 — clear every row past the header in the
    Google Sheet. Returns 0 on success / no-op (no creds), >0
    on failure. Header row stays — the bot rewrites it on next
    sync via `_ensure_headers`."""
    from app.config import get_settings
    from app.sync.factories import build_sheets_factory
    from app.sync.sheets import _HEADER_ROW, _col_letter

    settings = get_settings()
    factory = build_sheets_factory(settings)
    if factory is None:
        log_.warning("wipe_sheet_skipped_no_factory")
        return 0
    svc = factory()
    if svc is None:
        log_.warning("wipe_sheet_skipped_no_credentials")
        return 0
    try:
        end_col = _col_letter(len(_HEADER_ROW))
        rng = f"{svc._sheet_name}!A2:{end_col}"
        svc._service.spreadsheets().values().clear(
            spreadsheetId=svc._spreadsheet_id,
            range=rng,
            body={},
        ).execute()
        log_.info("wipe_sheet_cleared", range=rng)
        return 0
    except Exception as e:  # noqa: BLE001
        log_.error("wipe_sheet_failed", error=str(e))
        return 1


def main() -> int:
    setup_logging()
    args = _parse_args()
    dry_run = args.dry_run or not args.yes

    counts: dict[str, int] = {}
    with session_scope() as session:
        # Snapshot row counts (pre-wipe).
        counts["tasks"] = session.query(Task).count()
        counts["action_drafts"] = session.query(ActionDraft).count()
        counts["task_status_history"] = session.query(TaskStatusHistory).count()
        counts["task_subscriptions"] = session.query(TaskSubscription).count()
        counts["google_sheets_sync"] = session.query(GoogleSheetsSync).count()
        counts["google_tasks_sync"] = session.query(GoogleTasksSync).count()
        # daily_plan_items lives behind a soft import — model may not
        # exist on every branch.
        try:
            from app.models import DailyPlanItem

            counts["daily_plan_items"] = session.query(DailyPlanItem).count()
        except Exception:  # noqa: BLE001
            counts["daily_plan_items"] = 0
        counts["audit_logs"] = (
            session.query(AuditLog)
            .filter(AuditLog.category.in_(_WIPED_AUDIT_CATEGORIES))
            .count()
        )

        log.info("wipe_tasks_snapshot", dry_run=dry_run, **counts)
        if dry_run:
            log.info(
                "wipe_tasks_dry_run_done",
                hint="Re-run with --yes to actually wipe.",
            )
            return 0

        # Order matters under FK constraints. Children first.
        # task_status_history → tasks
        session.execute(delete(TaskStatusHistory))
        session.execute(delete(TaskSubscription))
        session.execute(delete(GoogleSheetsSync))
        session.execute(delete(GoogleTasksSync))
        try:
            from app.models import DailyPlanItem

            session.execute(delete(DailyPlanItem))
        except Exception:  # noqa: BLE001
            pass
        session.execute(delete(ActionDraft))
        session.execute(delete(Task))
        session.execute(
            delete(AuditLog).where(AuditLog.category.in_(_WIPED_AUDIT_CATEGORIES))
        )
        # session_scope commits on exit

    sheet_rc = 0
    if args.also_wipe_sheet:
        sheet_rc = _wipe_sheet_data(log_=log)
    log.info("wipe_tasks_done", sheet_clear_rc=sheet_rc, **counts)
    return sheet_rc


if __name__ == "__main__":
    sys.exit(main())
