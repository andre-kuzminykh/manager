"""FR-CR-04-28 — Telegram-side button handlers.

Tests cover:

- `handle_start` flips backlog/todo → in_progress, claims an
  unowned task for the actor, but rejects strangers when the task
  has an owner.
- `handle_done` transitions to done; only the owner can call it.
- `handle_cancel` routes by due_date (this week → todo, else
  backlog).
- `handle_delete` sets `deleted_at`, writes an audit row, owner-
  only.
- `handle_subscribe` adds / removes a TaskSubscription; the owner
  can't re-subscribe (no-op).
- `_route_on_cancel` routing rule.
"""
from __future__ import annotations

from datetime import date, datetime, time as _time, timedelta, timezone

import pytest

from app.models import (
    AuditLog,
    Task,
    TaskPriority,
    TaskSourceKind,
    TaskStatus,
    TaskSubscription,
)
from app.telegram_bot import handlers as h


def _mk(session, **kw) -> int:
    base = dict(
        title="t",
        priority=TaskPriority.medium,
        status=TaskStatus.in_progress,
        owner_user_id="11111",
        source_kind=TaskSourceKind.telegram,
    )
    base.update(kw)
    t = Task(**base)
    session.add(t)
    session.flush()
    return t.id


# --------------------------------------------------------------------------- #
# Permissions
# --------------------------------------------------------------------------- #


def test_ensure_can_edit_passes_for_owner(session):
    tid = _mk(session, owner_user_id="22222")
    task = session.get(Task, tid)
    h._ensure_can_edit(task, "22222")  # no raise


def test_ensure_can_edit_blocks_strangers(session):
    tid = _mk(session, owner_user_id="22222")
    task = session.get(Task, tid)
    with pytest.raises(h.NotAuthorised):
        h._ensure_can_edit(task, "99999")


# --------------------------------------------------------------------------- #
# Start
# --------------------------------------------------------------------------- #


def test_handle_start_owner_transitions_to_in_progress(session):
    tid = _mk(session, status=TaskStatus.todo, owner_user_id="11")
    task = h.handle_start(session, task_id=tid, actor="11")
    assert task is not None
    assert task.status == TaskStatus.in_progress


def test_handle_start_unowned_claims_for_actor(session):
    tid = _mk(session, status=TaskStatus.todo, owner_user_id=None)
    task = h.handle_start(session, task_id=tid, actor="42")
    assert task is not None
    assert task.owner_user_id == "42"
    assert task.status == TaskStatus.in_progress


def test_handle_start_stranger_rejected_when_owner_present(session):
    tid = _mk(session, status=TaskStatus.todo, owner_user_id="11")
    with pytest.raises(h.NotAuthorised):
        h.handle_start(session, task_id=tid, actor="99")


def test_handle_start_snaps_start_date_and_time_to_now(session):
    """FR-CR-05-69 — pressing Start sets `start_date` to today
    and `start_time` to the current time. The planning pair
    reflects when the work actually started, not whatever was
    pre-filled on the card."""
    from datetime import date as _date

    tid = _mk(session, status=TaskStatus.todo, owner_user_id="11")
    task = h.handle_start(session, task_id=tid, actor="11")
    assert task is not None
    assert task.start_date == _date.today()
    assert task.start_time is not None
    # 00:00 (default Time()) would mean we forgot to set it.
    assert task.start_time != _time(0, 0)


# --------------------------------------------------------------------------- #
# Done / Cancel
# --------------------------------------------------------------------------- #


def test_handle_done_transitions_to_done(session):
    tid = _mk(session, status=TaskStatus.in_progress, owner_user_id="11")
    task = h.handle_done(session, task_id=tid, actor="11")
    assert task.status == TaskStatus.done


def test_handle_cancel_within_week_routes_to_todo(session):
    today = date.today()
    tid = _mk(
        session,
        status=TaskStatus.in_progress,
        owner_user_id="11",
        due_date=today + timedelta(days=1),
    )
    task = h.handle_cancel(session, task_id=tid, actor="11")
    assert task.status == TaskStatus.todo


def test_handle_cancel_far_due_routes_to_backlog(session):
    today = date.today()
    tid = _mk(
        session,
        status=TaskStatus.in_progress,
        owner_user_id="11",
        due_date=today + timedelta(days=30),
    )
    task = h.handle_cancel(session, task_id=tid, actor="11")
    assert task.status == TaskStatus.backlog


def test_handle_cancel_blocks_stranger(session):
    tid = _mk(session, status=TaskStatus.in_progress, owner_user_id="11")
    with pytest.raises(h.NotAuthorised):
        h.handle_cancel(session, task_id=tid, actor="99")


# --------------------------------------------------------------------------- #
# Delete
# --------------------------------------------------------------------------- #


def test_handle_delete_soft_deletes_and_writes_audit(session):
    tid = _mk(session, owner_user_id="11", title="zap")
    task = h.handle_delete(session, task_id=tid, actor="11")
    assert task.deleted_at is not None
    rows = (
        session.query(AuditLog)
        .filter(AuditLog.action == "task_deleted")
        .all()
    )
    assert len(rows) == 1
    assert rows[0].payload["via"] == "telegram"
    assert rows[0].payload["title"] == "zap"


def test_handle_delete_blocks_stranger(session):
    tid = _mk(session, owner_user_id="11")
    with pytest.raises(h.NotAuthorised):
        h.handle_delete(session, task_id=tid, actor="99")
    task = session.get(Task, tid)
    assert task.deleted_at is None


# --------------------------------------------------------------------------- #
# Subscribe / unsubscribe
# --------------------------------------------------------------------------- #


def test_handle_subscribe_adds_row_for_bystander(session):
    tid = _mk(session, owner_user_id="11")
    h.handle_subscribe(session, task_id=tid, actor="42", subscribe=True)
    rows = session.query(TaskSubscription).filter_by(task_id=tid).all()
    sids = {r.slack_user_id for r in rows}
    assert "42" in sids


def test_handle_unsubscribe_removes_row(session):
    tid = _mk(session, owner_user_id="11")
    h.handle_subscribe(session, task_id=tid, actor="42", subscribe=True)
    h.handle_subscribe(session, task_id=tid, actor="42", subscribe=False)
    rows = (
        session.query(TaskSubscription)
        .filter_by(task_id=tid, slack_user_id="42")
        .all()
    )
    assert rows == []


def test_handle_subscribe_owner_is_noop(session):
    tid = _mk(session, owner_user_id="11")
    # No action — owner is implicitly subscribed at creation, the
    # button shouldn't appear for them, but if it did we don't
    # change anything.
    h.handle_subscribe(session, task_id=tid, actor="11", subscribe=True)
    h.handle_subscribe(session, task_id=tid, actor="11", subscribe=False)
    # No exception, no extra subscription rows.
    rows = (
        session.query(TaskSubscription)
        .filter_by(task_id=tid, slack_user_id="11")
        .all()
    )
    assert len(rows) <= 1


# --------------------------------------------------------------------------- #
# Edit help (MVP placeholder)
# --------------------------------------------------------------------------- #


def test_edit_help_text_contains_word_edit():
    text = h.handle_edit_help()
    assert "Edit" in text


# --------------------------------------------------------------------------- #
# Routing helper
# --------------------------------------------------------------------------- #


def test_route_on_cancel_no_due_date_goes_to_backlog():
    t = Task(title="t", priority=TaskPriority.medium, status=TaskStatus.in_progress)
    assert h._route_on_cancel(t) == TaskStatus.backlog


def test_route_on_cancel_within_week():
    today = date(2026, 4, 27)
    t = Task(
        title="t",
        priority=TaskPriority.medium,
        status=TaskStatus.in_progress,
        due_date=today + timedelta(days=2),
    )
    assert h._route_on_cancel(t, today=today) == TaskStatus.todo
