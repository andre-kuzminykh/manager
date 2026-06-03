"""FR-ST-LOG — unified append-only status-event log
(SPEC_STATUS_TRACKER_v0.2 §2). Every task field change writes one row with
from->to + source + actor; comment-only events allowed; meeting events are
idempotent; chat events never collide.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Task, TaskStatusEvent
from app.services.status_events import record_status_event


def _task(session, title: str = "T") -> Task:
    t = Task(title=title)
    session.add(t)
    session.flush()
    return t


def test_records_from_to(sqlite_session) -> None:
    t = _task(sqlite_session)
    record_status_event(
        sqlite_session, task_id=t.id, source="chat", field="status",
        from_value="todo", to_value="done", actor="U1",
    )
    sqlite_session.commit()
    ev = sqlite_session.query(TaskStatusEvent).one()
    assert ev.field == "status"
    assert ev.from_value == "todo"
    assert ev.to_value == "done"
    assert ev.source == "chat"
    assert ev.actor == "U1"
    assert ev.applied is True


def test_comment_only_event(sqlite_session) -> None:
    t = _task(sqlite_session)
    record_status_event(
        sqlite_session, task_id=t.id, source="zoom", field="comment",
        to_value=None, comment="отправил коммерческое", raw_quote="я вчера отправил",
        confidence=0.9, meeting_ref={"source_id": "zm_1", "segment_idx": 3},
    )
    sqlite_session.commit()
    ev = sqlite_session.query(TaskStatusEvent).one()
    assert ev.field == "comment"
    assert ev.comment == "отправил коммерческое"
    assert ev.to_value is None
    assert ev.meeting_source_id == "zm_1"
    assert ev.confidence == 0.9


def test_owner_dict_json_roundtrip(sqlite_session) -> None:
    t = _task(sqlite_session)
    record_status_event(
        sqlite_session, task_id=t.id, source="chat", field="owner",
        from_value={"owner_user_id": "U1", "owner_display_name": "A"},
        to_value={"owner_user_id": "U2", "owner_display_name": "B"},
    )
    sqlite_session.commit()
    ev = sqlite_session.query(TaskStatusEvent).one()
    assert json.loads(ev.to_value)["owner_display_name"] == "B"
    assert json.loads(ev.from_value)["owner_user_id"] == "U1"


def test_meeting_event_idempotent(sqlite_session) -> None:
    # FR-ST-LOG-2: same (source, source_id, segment_idx, task) → UNIQUE clash.
    t = _task(sqlite_session)
    ref = {"source_id": "ff_42", "segment_idx": 7}
    record_status_event(sqlite_session, task_id=t.id, source="fireflies",
                        field="status", to_value="done", meeting_ref=ref)
    sqlite_session.commit()
    # replay → UNIQUE clash on flush (record_status_event_safe swallows this
    # to a no-op in production; the raw recorder surfaces it to the caller).
    with pytest.raises(IntegrityError):
        record_status_event(sqlite_session, task_id=t.id, source="fireflies",
                            field="status", to_value="done", meeting_ref=ref)
    sqlite_session.rollback()


def test_chat_events_never_collide(sqlite_session) -> None:
    # NULL meeting columns are distinct in the UNIQUE index → many chat
    # events on one task coexist.
    t = _task(sqlite_session)
    for v in ("todo", "in_progress", "done"):
        record_status_event(sqlite_session, task_id=t.id, source="chat",
                            field="status", to_value=v)
    sqlite_session.commit()
    assert sqlite_session.query(TaskStatusEvent).count() == 3


__all__: list[str] = []
