"""Requirement coverage: FR-CR-04-20 (drop the `review` status, soft
delete, four-state lifecycle) and FR-CR-04-21 (Cancel / Delete buttons,
optional completion artifact).

Cancel routes a task back to `todo` if its due date is within the
current calendar week, otherwise to `backlog`. Delete is owner+admin
only, opens a confirmation modal, and performs a soft delete (sets
`tasks.deleted_at` and writes an audit row); the task is hidden
from queries afterwards.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.models import AuditLog, Task, TaskStatus, TaskSubscription
from app.slack_bot import blocks as bk
from app.slack_bot.handlers.task_actions import (
    _route_on_cancel,
    handle_cancel_task,
    handle_delete_task_open,
    handle_delete_task_submit,
)


def _mk(session, **kw) -> int:
    base = dict(
        title="t",
        status=TaskStatus.in_progress,
        owner_user_id="U-owner",
        card_channel="C1",
        card_ts="100.0",
    )
    base.update(kw)
    t = Task(**base)
    session.add(t)
    session.flush()
    return t.id


def _body(task_id: int, *, user="U-owner") -> dict:
    return {
        "actions": [{"value": str(task_id)}],
        "user": {"id": user},
        "channel": {"id": "C1"},
        "trigger_id": "trig-1",
    }


class _Sender:
    def __init__(self):
        self.posted = []
        self.updated = []
        self.ephemerals = []

    def post_message(self, **kw):
        self.posted.append(kw)
        return {"ok": True, "ts": "1.0"}

    def update_message(self, **kw):
        self.updated.append(kw)
        return {"ok": True}

    def post_ephemeral(self, **kw):
        self.ephemerals.append(kw)
        return {"ok": True}


class _Client:
    def __init__(self):
        self.opened = []

    def views_open(self, *, trigger_id, view):  # noqa: N802
        self.opened.append({"trigger_id": trigger_id, "view": view})


# --------------------------------------------------------------------------- #
# Cancel routing
# --------------------------------------------------------------------------- #


def test_route_on_cancel_within_week_goes_to_todo():
    today = date(2026, 4, 27)  # Monday
    t = Task(title="t", status=TaskStatus.in_progress, due_date=today + timedelta(days=2))
    assert _route_on_cancel(t, today=today) == TaskStatus.todo


def test_route_on_cancel_after_week_goes_to_backlog():
    today = date(2026, 4, 27)  # Monday
    t = Task(
        title="t",
        status=TaskStatus.in_progress,
        due_date=today + timedelta(days=14),
    )
    assert _route_on_cancel(t, today=today) == TaskStatus.backlog


def test_route_on_cancel_no_due_date_goes_to_backlog():
    t = Task(title="t", status=TaskStatus.in_progress)
    assert _route_on_cancel(t) == TaskStatus.backlog


def test_cancel_in_progress_task_routes_to_todo_when_close(
    patched_session_scope, SessionFactory, ack
):
    today = date.today()
    with SessionFactory() as s:
        tid = _mk(s, status=TaskStatus.in_progress, due_date=today + timedelta(days=1))
        s.commit()

    handle_cancel_task(body=_body(tid), sender=_Sender(), ack=ack)

    with SessionFactory() as s:
        t = s.get(Task, tid)
        assert t.status == TaskStatus.todo


def test_cancel_in_progress_task_routes_to_backlog_when_far(
    patched_session_scope, SessionFactory, ack
):
    today = date.today()
    with SessionFactory() as s:
        tid = _mk(s, status=TaskStatus.in_progress, due_date=today + timedelta(days=30))
        s.commit()

    handle_cancel_task(body=_body(tid), sender=_Sender(), ack=ack)

    with SessionFactory() as s:
        assert s.get(Task, tid).status == TaskStatus.backlog


def test_cancel_rejects_non_owner(patched_session_scope, SessionFactory, ack):
    with SessionFactory() as s:
        tid = _mk(s, status=TaskStatus.in_progress, owner_user_id="U-owner")
        s.commit()

    sender = _Sender()
    handle_cancel_task(body=_body(tid, user="U-stranger"), sender=sender, ack=ack)

    with SessionFactory() as s:
        # Status unchanged.
        assert s.get(Task, tid).status == TaskStatus.in_progress
    # Stranger got an ephemeral lock notice.
    assert sender.ephemerals
    assert "owner" in sender.ephemerals[0]["text"].lower()


# --------------------------------------------------------------------------- #
# Cancel button placement
# --------------------------------------------------------------------------- #


def test_cancel_button_visible_to_owner_when_in_progress():
    t = Task(
        id=1,
        title="t",
        status=TaskStatus.in_progress,
        owner_user_id="U-owner",
    )
    blocks = bk.task_card(task=t, viewer_slack_user_id="U-owner")
    ids = [el["action_id"] for b in blocks if b["type"] == "actions" for el in b["elements"]]
    assert bk.ACTION_CANCEL_TASK in ids


def test_cancel_button_hidden_for_non_owner():
    t = Task(
        id=1,
        title="t",
        status=TaskStatus.in_progress,
        owner_user_id="U-owner",
    )
    blocks = bk.task_card(task=t, viewer_slack_user_id="U-stranger")
    ids = [el["action_id"] for b in blocks if b["type"] == "actions" for el in b["elements"]]
    assert bk.ACTION_CANCEL_TASK not in ids


def test_cancel_button_hidden_when_already_backlog():
    t = Task(
        id=1,
        title="t",
        status=TaskStatus.backlog,
        owner_user_id="U-owner",
    )
    blocks = bk.task_card(task=t, viewer_slack_user_id="U-owner")
    ids = [el["action_id"] for b in blocks if b["type"] == "actions" for el in b["elements"]]
    assert bk.ACTION_CANCEL_TASK not in ids


# --------------------------------------------------------------------------- #
# Delete: confirmation modal + soft delete
# --------------------------------------------------------------------------- #


def test_delete_button_visible_to_owner_and_admin_only():
    t = Task(id=1, title="t", status=TaskStatus.todo, owner_user_id="U-owner")
    owner_blocks = bk.task_card(task=t, viewer_slack_user_id="U-owner")
    owner_ids = [
        el["action_id"]
        for b in owner_blocks
        if b["type"] == "actions"
        for el in b["elements"]
    ]
    assert bk.ACTION_DELETE_TASK in owner_ids

    other_blocks = bk.task_card(task=t, viewer_slack_user_id="U-other")
    other_ids = [
        el["action_id"]
        for b in other_blocks
        if b["type"] == "actions"
        for el in b["elements"]
    ]
    assert bk.ACTION_DELETE_TASK not in other_ids


def test_delete_open_renders_confirmation_modal(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        tid = _mk(s, title="kill me", status=TaskStatus.in_progress)
        s.commit()

    client = _Client()
    handle_delete_task_open(
        body=_body(tid), client=client, sender=_Sender(), ack=ack
    )
    assert client.opened
    view = client.opened[0]["view"]
    assert view["callback_id"] == bk.MODAL_CALLBACK_DELETE_TASK
    body_text = view["blocks"][0]["text"]["text"]
    assert "kill me" in body_text


def test_delete_open_blocks_non_owner(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        tid = _mk(s, owner_user_id="U-owner")
        s.commit()

    client = _Client()
    sender = _Sender()
    handle_delete_task_open(
        body=_body(tid, user="U-stranger"),
        client=client,
        sender=sender,
        ack=ack,
    )
    assert client.opened == []
    assert sender.ephemerals  # lock notice shown to the stranger


def test_delete_submit_marks_deleted_at_and_writes_audit(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        tid = _mk(s, title="goodbye", status=TaskStatus.in_progress)
        s.commit()

    view = {"private_metadata": str(tid)}
    handle_delete_task_submit(
        body={"user": {"id": "U-owner"}},
        view=view,
        sender=_Sender(),
        ack=ack,
    )

    with SessionFactory() as s:
        t = s.get(Task, tid)
        assert t is not None  # row preserved
        assert t.deleted_at is not None
        # Audit row recorded for traceability.
        log = (
            s.query(AuditLog)
            .filter(AuditLog.category == "task", AuditLog.action == "task_deleted")
            .first()
        )
        assert log is not None
        assert log.entity_id == str(tid)
        assert log.payload["title"] == "goodbye"
        assert log.payload["status_at_delete"] == "in_progress"


def test_delete_submit_replaces_card_with_tombstone(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        tid = _mk(s, card_channel="C1", card_ts="100.0")
        s.commit()

    sender = _Sender()
    view = {"private_metadata": str(tid)}
    handle_delete_task_submit(
        body={"user": {"id": "U-owner"}},
        view=view,
        sender=sender,
        ack=ack,
    )
    assert sender.updated
    upd = sender.updated[0]
    assert upd["channel"] == "C1"
    assert upd["ts"] == "100.0"
    rendered = upd["blocks"][0]["elements"][0]["text"]
    assert "deleted" in rendered.lower()


def test_delete_submit_blocks_non_owner(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        tid = _mk(s, owner_user_id="U-owner")
        s.commit()

    view = {"private_metadata": str(tid)}
    handle_delete_task_submit(
        body={"user": {"id": "U-stranger"}},
        view=view,
        sender=_Sender(),
        ack=ack,
    )
    with SessionFactory() as s:
        assert s.get(Task, tid).deleted_at is None


def test_admin_can_delete(patched_session_scope, SessionFactory, ack, monkeypatch):
    monkeypatch.setenv("ADMIN_SLACK_USER_IDS", "U-admin")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        with SessionFactory() as s:
            tid = _mk(s, owner_user_id="U-owner")
            s.commit()

        view = {"private_metadata": str(tid)}
        handle_delete_task_submit(
            body={"user": {"id": "U-admin"}},
            view=view,
            sender=_Sender(),
            ack=ack,
        )
        with SessionFactory() as s:
            assert s.get(Task, tid).deleted_at is not None
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Soft-deleted tasks are excluded from queries
# --------------------------------------------------------------------------- #


def test_soft_deleted_task_excluded_from_workload(session):
    from app.services.workload import WorkloadEstimator

    t1 = Task(
        title="alive",
        status=TaskStatus.todo,
        owner_user_id="U-owner",
        estimated_minutes=120,
    )
    t2 = Task(
        title="dead",
        status=TaskStatus.todo,
        owner_user_id="U-owner",
        estimated_minutes=600,
        deleted_at=datetime.now(timezone.utc),
    )
    session.add_all([t1, t2])
    session.flush()
    est = WorkloadEstimator()
    assert est.owner_backlog_minutes(session, "U-owner") == 120


def test_soft_deleted_task_excluded_from_daily_plan_candidates(session):
    from app.services.daily_plan import _candidate_tasks_for

    today = date.today()
    alive = Task(
        title="alive",
        status=TaskStatus.todo,
        owner_user_id="U1",
        is_current_week=True,
    )
    dead = Task(
        title="dead",
        status=TaskStatus.todo,
        owner_user_id="U1",
        is_current_week=True,
        deleted_at=datetime.now(timezone.utc),
    )
    session.add_all([alive, dead])
    session.flush()

    out = _candidate_tasks_for(session, "U1", today)
    titles = {t.title for t in out}
    assert "alive" in titles
    assert "dead" not in titles


# --------------------------------------------------------------------------- #
# Completion modal: both fields optional
# --------------------------------------------------------------------------- #


def test_complete_modal_both_inputs_marked_optional():
    view = bk.complete_task_modal(task_id=1)
    inputs = [b for b in view["blocks"] if b.get("type") == "input"]
    assert all(b.get("optional") is True for b in inputs)


# --------------------------------------------------------------------------- #
# Submit-for-review machinery is gone
# --------------------------------------------------------------------------- #


def test_submit_for_review_action_constant_removed():
    assert not hasattr(bk, "ACTION_SUBMIT_REVIEW")


def test_review_status_value_removed_from_enum():
    with pytest.raises(ValueError):
        TaskStatus("review")
