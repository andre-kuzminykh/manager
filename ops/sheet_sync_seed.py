"""Seed the Task structure into an empty Google Sheet tab (spec §20.2/§20.3).

Writes the header row, freezes it, and sets dropdown validations
(Status / Priority / Category / Responsible). Responsible options are pulled
from active team_members. Idempotent — safe to re-run.

The target sheet must be SHARED with the service-account email as Editor.

Usage:
    docker exec manager-zoom-ff-1 python -m ops.sheet_sync_seed \\
        --spreadsheet-id 1h1wCHmrmPm5iJl-5oxbZ3HMtwoRWkAO81BzVk4SJLOc \\
        --tab ceo_brain_tasks
"""
from __future__ import annotations

import argparse
import sys

from app.config import get_settings
from app.db import session_scope
from app.logging_setup import get_logger, setup_logging
from app.sheet_sync.sheets_client import SheetTabNotFound, TasksSheetClient
from app.services.team_members import get_humans_for_matcher

log = get_logger(__name__)


def main() -> int:
    setup_logging()
    s = get_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--spreadsheet-id",
        default=getattr(s, "sheet_sync_spreadsheet_id", "") or "",
    )
    ap.add_argument(
        "--tab", default=getattr(s, "sheet_sync_tab_title", "") or "ceo_brain_tasks"
    )
    ap.add_argument(
        "--no-responsible-dropdown",
        action="store_true",
        help="Не ставить dropdown по команде (если не нужен).",
    )
    args = ap.parse_args()

    if not args.spreadsheet_id:
        print(
            "ERROR: spreadsheet id не задан (--spreadsheet-id или "
            "SHEET_SYNC_SPREADSHEET_ID)",
            file=sys.stderr,
        )
        return 2

    responsible: list[str] = []
    if not args.no_responsible_dropdown:
        with session_scope() as session:
            seen: set[str] = set()
            for h in get_humans_for_matcher(session):
                rn = (h.get("real_name") or "").strip()
                if rn and rn not in seen:
                    seen.add(rn)
                    responsible.append(rn)
            session.rollback()  # read-only
        responsible.sort()

    client = TasksSheetClient(spreadsheet_id=args.spreadsheet_id, tab_title=args.tab)
    try:
        result = client.ensure_structure(responsible_options=responsible)
    except SheetTabNotFound as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 3

    print(f"\nСтруктура залита в '{client.spreadsheet_title}' / tab '{args.tab}'")
    print(f"  sheet_id(gid)={result['sheet_id']}, колонок={result['headers']}")
    print(f"  dropdown Responsible: {len(responsible)} человек из команды")
    print("  заголовки заморожены, dropdown: Status / Priority / Category"
          + (" / Responsible" if responsible else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
