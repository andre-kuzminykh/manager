"""FR-SS — apply a ReconcilePlan to the DB (+ S0 events) and drive the
Sheet writes through an injectable writer (SPEC_SHEET_SYNC_v0.1 §5/§6/§9).

The DB-mutating half is pure-ish (session + plan in, events + links out) and
unit-tested with sqlite + a fake writer. The Google Sheets calls live behind
the `SheetWriter` protocol; the real impl (B-IO) is a thin wrapper over
System B's sheets_client and is exercised only on prod / shadow tab.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Protocol

from app.logging_setup import get_logger
from app.models.sheet_task_link import SheetTaskLink
from app.services.status_events import record_status_event
from app.sheet_sync.bridge import ReconcilePlan, RowView, payload_hash

log = get_logger(__name__)

_STATUS_KEYS = {"backlog", "todo", "in_progress", "done"}


# ---------- adapters ----------

def task_payload(task: Any) -> dict[str, str]:
    """Canonical editable representation of a Task (for diff / push)."""
    extra = getattr(task, "extra", None) or {}
    return {
        "title": task.title or "",
        "description": (getattr(task, "description", None) or ""),
        "status": (getattr(task.status, "value", task.status) or ""),
        "priority": (getattr(task.priority, "value", task.priority) or ""),
        "owner": (getattr(task, "owner_display_name", None) or ""),
        "due_date": (task.due_date.isoformat() if getattr(task, "due_date", None) else ""),
        "category": (extra.get("direction") or ""),
    }


# ---------- Sheet write side (mockable) ----------

class SheetWriter(Protocol):
    def write_cells(self, task_id: int, fields: dict[str, str]) -> None: ...
    def append_task(self, task_id: int, payload: dict[str, str]) -> str: ...   # -> row_uuid
    def stamp_new_row(self, row_number: int, task_id: int) -> str: ...          # -> row_uuid
    def tombstone(self, task_id: int) -> None: ...


# ---------- DB mutations (each logs an S0 event) ----------

def _soft_delete(session: Any, task: Any, actor: str, reason: str) -> None:
    if task.deleted_at is not None:
        return
    task.deleted_at = _dt.datetime.utcnow()
    record_status_event(
        session, task_id=task.id, source="sheet", field="deleted",
        from_value=(getattr(task.status, "value", task.status) or ""),
        to_value="deleted", actor=actor, comment=reason,
    )


def _apply_status(session: Any, task: Any, value: str, actor: str) -> str | None:
    from app.models.task import TaskStatus
    from app.services.transitions import InvalidTransition, TransitionService

    key = (value or "").strip().lower().replace(" ", "_")
    if key in ("cancelled", "canceled"):          # FR-SS-MAP-2
        _soft_delete(session, task, actor, "sheet:cancelled")
        return None
    if key == "blocked":                          # FR-SS-MAP-3 (no enum) -> comment
        record_status_event(session, task_id=task.id, source="sheet",
                            field="comment", comment="blocked (from sheet)", actor=actor)
        return None
    if key not in _STATUS_KEYS:
        return "invalid_status"
    old = getattr(task.status, "value", task.status)
    if old == key:
        return None
    try:
        TransitionService().apply(session, task=task, new_status=TaskStatus(key),
                                  actor_slack_user_id=actor, reason="sheet_edit")
    except InvalidTransition as e:
        return f"invalid_transition:{e}"
    record_status_event(session, task_id=task.id, source="sheet", field="status",
                        from_value=old, to_value=key, actor=actor)
    return None


def apply_edit(session: Any, task: Any, field: str, value: str, actor: str) -> str | None:
    """Apply ONE field change from the sheet to the task + log it. Returns an
    error code or None."""
    if field == "status":
        return _apply_status(session, task, value, actor)
    if field == "title":
        old, task.title = (task.title or ""), value
        record_status_event(session, task_id=task.id, source="sheet", field="title",
                            from_value=old, to_value=value, actor=actor)
        return None
    if field == "description":
        old = getattr(task, "description", None) or ""
        task.description = value
        record_status_event(session, task_id=task.id, source="sheet", field="description",
                            from_value=old, to_value=value, actor=actor)
        return None
    if field == "priority":
        from app.models.task import TaskPriority
        try:
            pr = TaskPriority((value or "").strip().lower())
        except ValueError:
            return "invalid_priority"
        old = getattr(task.priority, "value", task.priority) or ""
        task.priority = pr
        record_status_event(session, task_id=task.id, source="sheet", field="priority",
                            from_value=old, to_value=pr.value, actor=actor)
        return None
    if field == "due_date":
        old = task.due_date.isoformat() if getattr(task, "due_date", None) else ""
        try:
            task.due_date = _dt.date.fromisoformat(value) if value else None
        except ValueError:
            return "invalid_date"
        record_status_event(session, task_id=task.id, source="sheet", field="due_date",
                            from_value=old, to_value=(value or ""), actor=actor)
        return None
    if field == "owner":
        old = {"owner_user_id": getattr(task, "owner_user_id", None),
               "owner_display_name": getattr(task, "owner_display_name", None)}
        task.owner_display_name = value  # id resolution is a separate concern (B-IO)
        new = {"owner_user_id": getattr(task, "owner_user_id", None),
               "owner_display_name": value}
        record_status_event(session, task_id=task.id, source="sheet", field="owner",
                            from_value=old, to_value=new, actor=actor)
        return None
    if field == "category":
        extra = dict(getattr(task, "extra", None) or {})
        old = extra.get("direction") or ""
        extra["direction"] = value
        task.extra = extra
        record_status_event(session, task_id=task.id, source="sheet", field="category",
                            from_value=old, to_value=value, actor=actor)
        return None
    return None


def apply_create(session: Any, row: RowView, actor: str) -> int:
    """Create a Task from a human-added sheet row. Returns new task_id."""
    from app.models.task import Task, TaskPriority, TaskStatus

    v = row.values
    t = Task(title=(v.get("title") or "").strip())
    skey = (v.get("status") or "todo").strip().lower().replace(" ", "_")
    if skey in _STATUS_KEYS:
        t.status = TaskStatus(skey)
    try:
        t.priority = TaskPriority((v.get("priority") or "").strip().lower())
    except (ValueError, Exception):  # noqa: BLE001
        pass
    try:
        t.due_date = _dt.date.fromisoformat(v["due_date"]) if v.get("due_date") else None
    except (ValueError, KeyError):
        pass
    if v.get("description"):
        t.description = v["description"]
    if v.get("owner"):
        t.owner_display_name = v["owner"]
    if v.get("category"):
        t.extra = {**(getattr(t, "extra", None) or {}), "direction": v["category"]}
    session.add(t)
    session.flush()
    record_status_event(session, task_id=t.id, source="sheet", field="created",
                        to_value=t.title, actor=actor)
    return t.id


# ---------- orchestrator ----------

def apply_plan(
    session: Any,
    plan: ReconcilePlan,
    *,
    spreadsheet_id: str,
    writer: SheetWriter,
    actor: str = "sheet",
) -> dict:
    """Apply a plan: DB mutations (+ S0 events + links) then Sheet writes via
    the injected writer. Returns a counts/errors summary."""
    res: dict[str, Any] = {"created": 0, "edited": 0, "deleted": 0,
                           "pushed": 0, "appended": 0, "errors": []}
    if plan.abort:
        res["abort"] = plan.abort
        return res

    from app.models.task import Task

    # --- creates (DB) + stamp identity back to the sheet ---
    for row in plan.creates:
        tid = apply_create(session, row, actor)
        uuid = writer.stamp_new_row(row.row_number, tid)
        session.add(SheetTaskLink(
            task_id=tid, spreadsheet_id=spreadsheet_id, row_uuid=uuid,
            row_number=row.row_number, last_payload_hash=payload_hash(row.values),
        ))
        res["created"] += 1

    # --- edits (DB) ---
    for tid, changes in plan.edits:
        t = session.get(Task, tid)
        if t is None or t.deleted_at is not None:
            continue
        for f, val in changes.items():
            err = apply_edit(session, t, f, val, actor)
            if err:
                res["errors"].append((tid, f, err))
        res["edited"] += 1

    # --- deletes (DB) ---
    for tid in plan.deletes:
        t = session.get(Task, tid)
        if t is not None and t.deleted_at is None:
            _soft_delete(session, t, actor, "sheet:row_removed")
            res["deleted"] += 1

    session.commit()

    # --- pushes (Sheet) — only differing cells ---
    for tid, cells in plan.pushes:
        writer.write_cells(tid, cells)
        res["pushed"] += 1

    # --- appends (Sheet) — live DB task w/o a row ---
    for tid in plan.appends:
        t = session.get(Task, tid)
        if t is None:
            continue
        pl = task_payload(t)
        uuid = writer.append_task(tid, pl)
        session.add(SheetTaskLink(
            task_id=tid, spreadsheet_id=spreadsheet_id, row_uuid=uuid,
            last_payload_hash=payload_hash(pl),
        ))
        res["appended"] += 1

    session.commit()
    return res


__all__ = [
    "task_payload", "SheetWriter", "apply_edit", "apply_create",
    "apply_plan",
]
