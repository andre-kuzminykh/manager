"""One-off: export the strategic morning-digest tasks INTO the Google Sheet.

DB → Sheet. Reads proposed `action_drafts` (the same set the strategic digest
uses), keeps DIRECTIONS_IMPORTANT, maps each to the Task columns and APPENDS
them below the header. Writes ONLY to the Sheet (no gs_* tables, no existing
tables touched). New rows carry no DeveloperMetadata yet — the first sync run
will pick them up as new records and stamp ids.

Usage:
    docker exec manager-zoom-ff-1 python -m ops.sheet_sync_export_tasks \\
        --spreadsheet-id 1h1wCHmrmPm5iJl-5oxbZ3HMtwoRWkAO81BzVk4SJLOc \\
        --tab main --since 2026-05-22
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.logging_setup import get_logger, setup_logging
from app.models.intent import ActionDraft, ActionDraftState
from app.services.task_direction import DIRECTIONS_IMPORTANT
from app.services.team_members import get_humans_for_matcher
from app.sheet_sync.config import TASK_HEADERS
from app.sheet_sync.sheets_client import SheetTabNotFound, TasksSheetClient

log = get_logger(__name__)

_PRIORITY_DISPLAY = {"low": "Low", "medium": "Medium", "high": "High", "urgent": "High"}


def _owner_resolvers(session):
    by_username, by_name = {}, {}
    valid_names: set[str] = set()
    for h in get_humans_for_matcher(session):
        rn = (h.get("real_name") or "").strip()
        if not rn:
            continue
        valid_names.add(rn)
        u = (h.get("tg_username") or "").strip().lower()
        if u:
            by_username[u] = rn
        by_name.setdefault(rn.lower(), rn)
        first = rn.lower().split()[0] if rn.split() else ""
        if first:
            by_name.setdefault(first, rn)
    return by_username, by_name, valid_names


def _resolve_owner(raw, by_username, by_name, valid_names):
    """Resolve to a REAL team_members name that exists in the Responsible
    dropdown list. If it doesn't resolve to a known team member → '' (blank),
    so every Responsible cell is a valid dropdown value (из таблицы людей)."""
    from ops.strategic_tasks_digest import _normalize_owner

    name = _normalize_owner(raw or "", by_username=by_username, by_name=by_name)
    return name if name in valid_names else ""


def main() -> int:
    setup_logging()
    s = get_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("--spreadsheet-id", default=getattr(s, "sheet_sync_spreadsheet_id", "") or "")
    ap.add_argument("--tab", default=getattr(s, "sheet_sync_tab_title", "") or "main")
    ap.add_argument("--since", default="2026-05-22")
    ap.add_argument("--status", default="To Do", help="Статус для всех залитых задач.")
    ap.add_argument("--replace", action="store_true",
                    help="Очистить строки данных перед заливкой (идемпотентно, без дублей).")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.spreadsheet_id:
        print("ERROR: spreadsheet id не задан", file=sys.stderr)
        return 2
    try:
        since_dt = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    except ValueError:
        print(f"ERROR: bad --since {args.since!r}", file=sys.stderr)
        return 2

    rows: list[list[str]] = []
    responsible_options: list[str] = []
    skipped_no_title = 0
    with session_scope() as session:
        by_username, by_name, valid_names = _owner_resolvers(session)
        responsible_options = sorted(valid_names)
        drafts = session.execute(
            select(ActionDraft)
            .where(ActionDraft.state == ActionDraftState.proposed)
            .where(ActionDraft.created_at >= since_dt)
            .order_by(ActionDraft.id.asc())
        ).scalars().all()
        for d in drafts:
            p = d.payload or {}
            direction = (p.get("direction") or "").strip().lower()
            if direction not in DIRECTIONS_IMPORTANT:
                continue
            title = (p.get("title") or "").strip()
            if not title:
                skipped_no_title += 1
                continue
            owner = _resolve_owner(p.get("owner_display_name"), by_username, by_name, valid_names)
            priority = _PRIORITY_DISPLAY.get((p.get("priority") or "medium").lower(), "Medium")
            category = direction.capitalize()
            due = (p.get("due_date") or "").strip()
            # TASK_HEADERS order: title, description, responsible, status, priority,
            # category, start_date, start_time, deadline_date, deadline_time,
            # completed_date, completed_time, comments
            rows.append([
                title,
                (p.get("description") or "").strip(),
                owner,
                args.status,
                priority,
                category,
                "", "",          # start date/time
                due, "",         # deadline date/time
                "", "",          # completion date/time
                "",              # comments
            ])
        session.rollback()  # read-only on the DB

    print(f"strategic-задач к заливке: {len(rows)} (с {args.since}, фильтр {DIRECTIONS_IMPORTANT}; "
          f"пропущено без title: {skipped_no_title})")
    assert len(TASK_HEADERS) == 13
    if args.dry_run:
        print("  #  | Category     | Prio   | Responsible          | Title")
        for i, r in enumerate(rows, 1):
            print(f"  {i:3d} | {r[5]:<12} | {r[4]:<6} | {(r[2] or '—'):<20} | {r[0][:70]}")
        print(f"  (--dry-run, в лист НЕ пишу) — всего {len(rows)}")
        return 0
    if not rows:
        print("нет задач — нечего заливать.")
        return 0

    client = TasksSheetClient(spreadsheet_id=args.spreadsheet_id, tab_title=args.tab)
    try:
        if args.replace:
            client.clear_data_rows()
            print("строки данных очищены (--replace).")
        n = client.append_rows(rows)
        # Re-apply structure so EVERY row (incl. just-appended) carries the
        # Status/Priority/Category/Responsible dropdowns → редактируемо списком.
        client.ensure_structure(responsible_options=responsible_options)
    except SheetTabNotFound as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 3
    print(f"залито строк: {n} в '{client.spreadsheet_title}' / tab '{args.tab}'")
    print("дропдауны (Status/Priority/Category/Responsible) переприменены на все строки.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
