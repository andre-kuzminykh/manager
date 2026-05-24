"""Runner for the Google Sheets Versioned Sync (Task entity).

ISOLATED: writes only gs_* tables + the configured Sheet. Behind
SHEET_SYNC_ENABLED. One-shot (`--sync-now`) or poll loop.

Usage:
    # one-shot (manual / testing), works regardless of the flag:
    docker exec manager-zoom-ff-1 python -m ops.sheet_sync_runner --sync-now \\
        --spreadsheet-id <id> --tab main
    # poll loop (separate container, needs SHEET_SYNC_ENABLED=true):
    python -m ops.sheet_sync_runner --loop
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.logging_setup import get_logger, setup_logging
from app.models.sheet_sync import GsSheetIntegration, GsSyncRun
from app.services.team_members import get_humans_for_matcher
from app.sheet_sync.engine import run_sync
from app.sheet_sync.repo import SqlSyncRepo
from app.sheet_sync.sheets_client import SheetTabNotFound, TasksSheetClient

log = get_logger(__name__)


def _build_assignee_resolver(session):
    by_name: dict[str, list[int]] = defaultdict(list)
    for h in get_humans_for_matcher(session):
        rn = (h.get("real_name") or "").strip().lower()
        tid = h.get("tm_id")
        if rn and tid is not None:
            by_name[rn].append(int(tid))

    def resolve(name: str):
        ids = by_name.get((name or "").strip().lower())
        if not ids:
            return ("unknown", None)
        if len(ids) > 1:
            return ("ambiguous", None)
        return ("ok", ids[0])

    return resolve


def _get_or_create_integration(session, *, spreadsheet_id, sheet_id, sheet_title, tz, interval):
    integ = session.execute(
        select(GsSheetIntegration)
        .where(GsSheetIntegration.spreadsheet_id == spreadsheet_id)
        .where(GsSheetIntegration.sheet_id == sheet_id)
    ).scalar_one_or_none()
    if integ is None:
        integ = GsSheetIntegration(
            entity_type="task", spreadsheet_id=spreadsheet_id, sheet_id=sheet_id,
            sheet_title=sheet_title, status="active", timezone=tz,
            sync_interval_seconds=interval,
        )
        session.add(integ)
        session.flush()
    return integ


def sync_once(*, spreadsheet_id: str, tab: str, tz: str, interval: int, trigger: str) -> dict:
    client = TasksSheetClient(spreadsheet_id=spreadsheet_id, tab_title=tab)
    sheet_id = client.resolve_tab()
    rows = client.read_rows()
    uuids = client.read_row_uuids()
    merged = [{**r, "row_uuid": uuids.get(r["row_number"])} for r in rows]

    with session_scope() as session:
        integ = _get_or_create_integration(
            session, spreadsheet_id=spreadsheet_id, sheet_id=sheet_id,
            sheet_title=tab, tz=tz, interval=interval,
        )
        run = GsSyncRun(integration_id=integ.id, status="running", trigger_type=trigger, rows_read=len(merged))
        session.add(run)
        session.flush()
        repo = SqlSyncRepo(session, integration_id=integ.id, sync_run_id=run.id)
        resolver = _build_assignee_resolver(session)
        try:
            stats = run_sync(merged, repo=repo, resolve_assignee=resolver, tz=tz)
        except Exception as e:  # noqa: BLE001
            run.status = "failed"
            run.error_type = type(e).__name__
            run.error_message = str(e)[:1000]
            run.finished_at = datetime.now(timezone.utc)
            session.commit()
            log.warning("sheet_sync_run_failed", error=str(e))
            raise
        run.created_count = stats.created
        run.updated_count = stats.updated
        run.deleted_count = stats.deleted
        run.unchanged_count = stats.unchanged
        run.error_count = stats.errors
        run.status = "completed"
        run.finished_at = datetime.now(timezone.utc)
        integ.last_sync_run_id = run.id
        session.commit()
        result = {
            "created": stats.created, "updated": stats.updated,
            "deleted": stats.deleted, "unchanged": stats.unchanged,
            "errors": stats.errors, "writebacks": list(stats.writebacks),
        }

    # Stamp DeveloperMetadata for brand-new rows (after DB commit succeeded).
    for row_number, new_uuid in result["writebacks"]:
        client.stamp_row_uuid(row_number, new_uuid)
    return result


def main() -> int:
    setup_logging()
    s = get_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("--spreadsheet-id", default=getattr(s, "sheet_sync_spreadsheet_id", "") or "")
    ap.add_argument("--tab", default=getattr(s, "sheet_sync_tab_title", "") or "main")
    ap.add_argument("--sync-now", action="store_true", help="Один прогон и выход.")
    ap.add_argument("--loop", action="store_true", help="Цикл опроса (нужен SHEET_SYNC_ENABLED).")
    args = ap.parse_args()

    tz = getattr(s, "sheet_sync_timezone", "UTC") or "UTC"
    interval = int(getattr(s, "sheet_sync_interval_seconds", 300) or 300)

    if not args.spreadsheet_id:
        print("ERROR: spreadsheet id не задан (--spreadsheet-id / SHEET_SYNC_SPREADSHEET_ID)", file=sys.stderr)
        return 2

    def _one(trigger: str):
        try:
            res = sync_once(spreadsheet_id=args.spreadsheet_id, tab=args.tab, tz=tz,
                            interval=interval, trigger=trigger)
            print(f"sync: {res}")
        except SheetTabNotFound as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 3
        return 0

    if args.loop:
        if not getattr(s, "sheet_sync_enabled", False):
            print("SHEET_SYNC_ENABLED=false — loop отключён, выходим.", file=sys.stderr)
            return 0
        log.info("sheet_sync_loop_start", interval=interval, tab=args.tab)
        while True:
            try:
                _one("scheduled")
            except Exception as e:  # noqa: BLE001
                log.warning("sheet_sync_loop_iter_error", error=str(e))
            time.sleep(interval)

    return _one("manual") or 0


if __name__ == "__main__":
    sys.exit(main())
