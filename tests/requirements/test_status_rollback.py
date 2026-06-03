"""FR-ST-RB — rollback from the unified event log
(SPEC_STATUS_TRACKER_v0.2 §3). rollback_event restores an event's from_value
via the normal write path and logs a NEW source=rollback event.
"""
from __future__ import annotations

import datetime as dt

from app.models import Task, TaskStatusEvent
from app.models.task import TaskStatus
from app.services.status_events import record_status_event, rollback_event


def test_rollback_due_date(sqlite_session) -> None:
    t = Task(title="T")
    t.due_date = dt.date(2026, 6, 4)
    sqlite_session.add(t)
    sqlite_session.flush()
    # simulate an update: 06-04 -> 06-10
    ev = record_status_event(sqlite_session, task_id=t.id, source="chat",
                             field="due_date", from_value="2026-06-04",
                             to_value="2026-06-10")
    t.due_date = dt.date(2026, 6, 10)
    sqlite_session.commit()

    res = rollback_event(sqlite_session, event_id=ev.id, sync=None)
    assert res["ok"] is True
    assert res["restored_to"] == "2026-06-04"
    sqlite_session.refresh(t)
    assert t.due_date == dt.date(2026, 6, 4)
    rb = sqlite_session.query(TaskStatusEvent).filter(
        TaskStatusEvent.source == "rollback").one()
    assert rb.field == "due_date"
    assert rb.to_value == "2026-06-04"


def test_rollback_status(sqlite_session) -> None:
    t = Task(title="T", status=TaskStatus.todo)
    sqlite_session.add(t)
    sqlite_session.flush()
    ev = record_status_event(sqlite_session, task_id=t.id, source="chat",
                             field="status", from_value="todo", to_value="done")
    t.status = TaskStatus.done
    sqlite_session.commit()

    res = rollback_event(sqlite_session, event_id=ev.id, sync=None)
    assert res["ok"] is True
    sqlite_session.refresh(t)
    assert getattr(t.status, "value", t.status) == "todo"


def test_rollback_owner(sqlite_session) -> None:
    import json

    t = Task(title="T")
    t.owner_user_id = "U2"
    t.owner_display_name = "B"
    sqlite_session.add(t)
    sqlite_session.flush()
    ev = record_status_event(
        sqlite_session, task_id=t.id, source="chat", field="owner",
        from_value={"owner_user_id": "U1", "owner_display_name": "A"},
        to_value={"owner_user_id": "U2", "owner_display_name": "B"},
    )
    sqlite_session.commit()

    res = rollback_event(sqlite_session, event_id=ev.id, sync=None)
    assert res["ok"] is True
    sqlite_session.refresh(t)
    assert t.owner_user_id == "U1"
    assert t.owner_display_name == "A"
    assert json.loads(  # rollback event records what it restored
        sqlite_session.query(TaskStatusEvent)
        .filter(TaskStatusEvent.source == "rollback").one().to_value
    )["owner_display_name"] == "A"


def test_rollback_missing_event(sqlite_session) -> None:
    res = rollback_event(sqlite_session, event_id=999999, sync=None)
    assert res["ok"] is False
    assert res["error"] == "event_not_found"


def test_comment_not_rollbackable(sqlite_session) -> None:
    t = Task(title="T")
    sqlite_session.add(t)
    sqlite_session.flush()
    ev = record_status_event(sqlite_session, task_id=t.id, source="zoom",
                             field="comment", comment="note")
    sqlite_session.commit()
    res = rollback_event(sqlite_session, event_id=ev.id, sync=None)
    assert res["ok"] is False
    assert "field_not_rollbackable" in res["error"]


__all__: list[str] = []
