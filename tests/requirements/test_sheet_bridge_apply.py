"""FR-SS — apply a ReconcilePlan to the DB + drive Sheet writes through a
fake writer (SPEC_SHEET_SYNC_v0.1 §5/§6/§9). Every DB change logs an S0
event (source=sheet) → rollbackable.
"""
from __future__ import annotations

import datetime as dt

from app.models import SheetTaskLink, Task, TaskStatusEvent
from app.models.task import TaskStatus
from app.sheet_sync.bridge import ReconcilePlan, RowView
from app.sheet_sync.bridge_apply import apply_create, apply_edit, apply_plan


class FakeWriter:
    def __init__(self) -> None:
        self.cells: list = []
        self.appends: list = []
        self.stamps: list = []
        self.tombstones: list = []
        self._n = 0

    def _uuid(self) -> str:
        self._n += 1
        return f"uuid-{self._n}"

    def write_cells(self, task_id, fields):
        self.cells.append((task_id, fields))

    def append_task(self, task_id, payload):
        u = self._uuid(); self.appends.append((task_id, u)); return u

    def stamp_new_row(self, row_number, task_id):
        u = self._uuid(); self.stamps.append((row_number, task_id, u)); return u

    def tombstone(self, task_id):
        self.tombstones.append(task_id)


def _task(session, **kw) -> Task:
    t = Task(title=kw.pop("title", "T"), **kw)
    session.add(t); session.flush(); return t


def _events(session, task_id, field=None):
    q = session.query(TaskStatusEvent).filter(TaskStatusEvent.task_id == task_id)
    if field:
        q = q.filter(TaskStatusEvent.field == field)
    return q.all()


def test_apply_edit_status_logs_event(sqlite_session) -> None:
    t = _task(sqlite_session, status=TaskStatus.todo)
    err = apply_edit(sqlite_session, t, "status", "done", actor="sheet")
    sqlite_session.commit()
    assert err is None
    assert getattr(t.status, "value", t.status) == "done"
    ev = _events(sqlite_session, t.id, "status")[0]
    assert ev.source == "sheet" and ev.from_value == "todo" and ev.to_value == "done"


def test_apply_edit_cancelled_soft_deletes(sqlite_session) -> None:
    t = _task(sqlite_session, status=TaskStatus.todo)
    apply_edit(sqlite_session, t, "status", "Cancelled", actor="sheet")
    sqlite_session.commit()
    assert t.deleted_at is not None
    assert _events(sqlite_session, t.id, "deleted")


def test_apply_edit_due_and_description(sqlite_session) -> None:
    t = _task(sqlite_session)
    apply_edit(sqlite_session, t, "due_date", "2026-06-10", actor="sheet")
    apply_edit(sqlite_session, t, "description", "new desc", actor="sheet")
    sqlite_session.commit()
    assert t.due_date == dt.date(2026, 6, 10)
    assert t.description == "new desc"
    assert _events(sqlite_session, t.id, "due_date") and _events(sqlite_session, t.id, "description")


def test_apply_edit_invalid_date_returns_error(sqlite_session) -> None:
    t = _task(sqlite_session)
    err = apply_edit(sqlite_session, t, "due_date", "not-a-date", actor="sheet")
    assert err == "invalid_date"


def test_apply_create_makes_task(sqlite_session) -> None:
    row = RowView(None, 5, {"title": "Заведена руками", "status": "todo",
                            "priority": "", "description": "", "owner": "",
                            "due_date": "", "category": ""})
    tid = apply_create(sqlite_session, row, actor="sheet")
    sqlite_session.commit()
    t = sqlite_session.get(Task, tid)
    assert t.title == "Заведена руками"
    assert _events(sqlite_session, tid, "created")


def test_apply_plan_end_to_end(sqlite_session) -> None:
    a = _task(sqlite_session, title="A", status=TaskStatus.todo, description="old")
    b = _task(sqlite_session, title="B", status=TaskStatus.todo)
    c = _task(sqlite_session, title="C", status=TaskStatus.todo)
    plan = ReconcilePlan(
        creates=[RowView(None, 9, {"title": "New", "status": "todo", "priority": "",
                                   "description": "", "owner": "", "due_date": "", "category": ""})],
        edits=[(a.id, {"status": "done"})],
        deletes=[b.id],
        pushes=[(a.id, {"description": "db desc"})],
        appends=[c.id],
    )
    w = FakeWriter()
    res = apply_plan(sqlite_session, plan, spreadsheet_id="SS1", writer=w, actor="sheet")

    assert res == {"created": 1, "edited": 1, "deleted": 1, "pushed": 1,
                   "appended": 1, "errors": []}
    # DB effects
    sqlite_session.refresh(a); sqlite_session.refresh(b)
    assert getattr(a.status, "value", a.status) == "done"
    assert b.deleted_at is not None
    # writer driven
    assert w.stamps and w.stamps[0][0] == 9            # stamped the new row
    assert w.cells == [(a.id, {"description": "db desc"})]
    assert w.appends and w.appends[0][0] == c.id
    # links persisted for create + append
    links = {l.task_id for l in sqlite_session.query(SheetTaskLink).all()}
    assert c.id in links and len(links) == 2           # created task + appended c


__all__: list[str] = []
