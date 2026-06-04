"""FR-SS — bidirectional Sheet<->DB bridge runner (SPEC_SHEET_SYNC_v0.1 §15).

ONE-SHOT mode (default):  reads the tab, plans, applies (DB + S0 events +
                          Sheet writes via batchUpdate). Idempotent.
--loop:                   every SHEET_SYNC_INTERVAL_SECONDS (default 600 = 10m)
                          tick. Single-writer (advisory lock on the spreadsheet).
--migrate:                one-shot — stamp DeveloperMetadata gs_row_uuid onto
                          every existing System A row that has a visible task_id
                          in column A, and create sheet_task_links rows. After
                          a successful --migrate the live bridge can be turned
                          on; System A writer should be disabled separately
                          (do not run two writers against the same tab).
--dry-run:                read + plan + print counts, NO writes (DB or Sheet).

SAFETY:
- gated by SHEET_SYNC_BRIDGE_ENABLED — exits with code 2 if off (no surprises);
- empty/error Sheet read aborts (FR-SS-SAFE-1);
- mass-delete guard (FR-SS-SAFE-2) aborts the tick.
"""
from __future__ import annotations

import argparse
import sys
from typing import Any

from app.config import get_settings
from app.db import session_scope
from app.logging_setup import get_logger, setup_logging
from app.models import SheetTaskLink, Task
from app.sheet_sync.bridge import plan_reconcile
from app.sheet_sync.bridge_apply import apply_plan, task_payload
from app.sheet_sync.bridge_io import GoogleSheetWriter, read_all_with_metadata

log = get_logger(__name__)


def _build_client(s: Any, tab: str | None = None):
    from app.sheet_sync.sheets_client import TasksSheetClient

    return TasksSheetClient(
        spreadsheet_id=s.sheet_sync_spreadsheet_id,
        tab_title=tab or s.sheet_sync_tab_title,
    )


def _gather_db_state(session: Any, spreadsheet_id: str) -> tuple[dict, dict, dict, dict]:
    """Live task payloads + uuid->task links + per-task updated_at + per-link synced_at."""
    tasks = session.query(Task).filter(Task.deleted_at.is_(None)).all()
    payloads = {t.id: task_payload(t) for t in tasks}
    updated_at = {t.id: getattr(t, "updated_at", None) for t in tasks}
    links_rows = (
        session.query(SheetTaskLink)
        .filter(SheetTaskLink.spreadsheet_id == spreadsheet_id)
        .all()
    )
    links = {l.row_uuid: (l.task_id, l.last_payload_hash) for l in links_rows}
    synced_at = {l.row_uuid: l.last_synced_at for l in links_rows}
    return payloads, links, updated_at, synced_at


def _tick(args: argparse.Namespace) -> int:
    s = get_settings()
    if not s.sheet_sync_bridge_enabled and not args.force:
        print("SHEET_SYNC_BRIDGE_ENABLED is off; pass --force to override.", file=sys.stderr)
        return 2
    if not s.sheet_sync_spreadsheet_id:
        print("SHEET_SYNC_SPREADSHEET_ID is empty.", file=sys.stderr)
        return 2

    client = _build_client(s, tab=getattr(args, "tab", None))
    client.resolve_tab()  # fail fast with available tabs if the tab is wrong
    rows = read_all_with_metadata(client)
    log.info("sheet_bridge_read", rows=len(rows),
             with_uuid=sum(1 for r in rows if r.row_uuid))

    with session_scope() as sess:
        payloads, links, upd_at, syn_at = _gather_db_state(sess, s.sheet_sync_spreadsheet_id)
        plan = plan_reconcile(
            rows, payloads, links,
            max_delete_pct=s.sheet_sync_bridge_max_delete_pct,
            task_updated_at=upd_at,
            link_synced_at=syn_at,
        )
        if plan.abort:
            log.warning("sheet_bridge_abort", reason=plan.abort)
            print(f"abort: {plan.abort}")
            return 1
        # --limit: safety cap on operations per tick (smoke / blast-radius).
        if args.limit and args.limit > 0:
            n = args.limit
            plan.creates = plan.creates[:n]
            plan.edits = plan.edits[:n]
            plan.deletes = plan.deletes[:n]
            plan.pushes = plan.pushes[:n]
            plan.appends = plan.appends[:n]
        print(f"plan: creates={len(plan.creates)} edits={len(plan.edits)} "
              f"deletes={len(plan.deletes)} pushes={len(plan.pushes)} "
              f"appends={len(plan.appends)} errors={len(plan.errors)} "
              f"stale={len(plan.stale_skipped)}")
        if args.dry_run:
            return 0
        writer = GoogleSheetWriter(client)
        res = apply_plan(sess, plan, spreadsheet_id=s.sheet_sync_spreadsheet_id,
                         writer=writer, actor="sheet")
        print(f"applied: {res}")
        return 0


def _migrate(args: argparse.Namespace) -> int:
    """Stamp gs_row_uuid + create sheet_task_links for legacy rows that carry a
    visible task_id in column A (System A layout). Safe to re-run."""
    import uuid as _uuid

    s = get_settings()
    if not s.sheet_sync_spreadsheet_id:
        print("SHEET_SYNC_SPREADSHEET_ID is empty.", file=sys.stderr)
        return 2

    # System A header has task_id in col A; this command lives in the bridge
    # tab — if the user runs --migrate against a System B tab (no visible id),
    # we just skip rows without a parseable id.
    from app.sheet_sync.sheets_client import TasksSheetClient

    client = TasksSheetClient(spreadsheet_id=s.sheet_sync_spreadsheet_id,
                              tab_title=args.tab or s.sheet_sync_tab_title)
    client.resolve_tab()  # raises SheetTabNotFound with available tabs if missing
    from app.sheet_sync.bridge_io import _a1_tab
    rng = f"{_a1_tab(client._tab)}!A1:A"
    resp = (client._svc.spreadsheets().values()
            .get(spreadsheetId=client._sid, range=rng).execute())
    rows = list(resp.get("values") or [])
    existing_uuids = client.read_row_uuids() or {}

    to_stamp: dict[int, str] = {}
    pairs: list[tuple[int, int, str]] = []
    for idx, row in enumerate(rows[1:], start=2):
        cell = (row[0] if row else "").strip()
        if not cell.isdigit():
            continue
        tid = int(cell)
        if idx in existing_uuids:
            pairs.append((idx, tid, existing_uuids[idx]))
            continue
        u = _uuid.uuid4().hex
        to_stamp[idx] = u
        pairs.append((idx, tid, u))

    print(f"migrate: rows={len(rows)-1} pairs={len(pairs)} new_uuids={len(to_stamp)}")
    if args.dry_run:
        return 0
    if to_stamp:
        client.stamp_row_uuids(to_stamp)
    with session_scope() as sess:
        for row_n, tid, u in pairs:
            existing = (sess.query(SheetTaskLink)
                        .filter(SheetTaskLink.spreadsheet_id == s.sheet_sync_spreadsheet_id,
                                SheetTaskLink.task_id == tid).one_or_none())
            if existing is None:
                sess.add(SheetTaskLink(
                    task_id=tid, spreadsheet_id=s.sheet_sync_spreadsheet_id,
                    row_uuid=u, row_number=row_n,
                ))
            else:
                existing.row_uuid = u
                existing.row_number = row_n
        sess.commit()
    print("done.")
    return 0


def main() -> int:
    setup_logging()
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--migrate", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="override SHEET_SYNC_BRIDGE_ENABLED guard")
    ap.add_argument("--limit", type=int, default=0, help="cap operations per tick (smoke/safety)")
    ap.add_argument("--tab", default=None, help="tab to scan/sync (overrides SHEET_SYNC_TAB_TITLE)")
    a = ap.parse_args()

    if a.migrate:
        return _migrate(a)

    if not a.loop:
        return _tick(a)

    import time
    s = get_settings()
    interval = max(60, int(s.sheet_sync_interval_seconds))
    print(f"loop: every {interval}s; bridge_enabled={s.sheet_sync_bridge_enabled}")
    while True:
        try:
            _tick(a)
        except Exception as e:  # noqa: BLE001 — never break the loop
            log.error("sheet_bridge_tick_failed", error=str(e))
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
