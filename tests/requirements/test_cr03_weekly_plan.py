"""Requirement coverage: FR-CR-03-6 (Sunday weekly plan),
NFR-CR-03-3 (digests idempotent per user+week).

CR-03 Phase D: Sunday weekly plan."""
from __future__ import annotations

from datetime import date, timedelta

from app.models import AuditLog, Task, TaskStatus, TaskStatusHistory
from app.services import send_weekly_plan
from app.services.weekly_plan import _week_bounds
from app.slack_bot import blocks as bk
from app.slack_bot.handlers.weekly_plan import (
    handle_weekly_accept,
    handle_weekly_defer,
)


class _S:
    def __init__(self):
        self.posts: list[dict] = []

    def post_message(self, **kw):
        self.posts.append(kw)
        return {"ok": True, "ts": "0"}


# --------------------------------------------------------------------------- #
# _week_bounds
# --------------------------------------------------------------------------- #


def test_week_bounds_from_sunday_looks_at_next_week():
    # 2026-04-26 is Sunday. Upcoming Mon = 2026-04-27, Sun = 2026-05-03.
    mon, sun = _week_bounds(date(2026, 4, 26))
    assert mon == date(2026, 4, 27)
    assert sun == date(2026, 5, 3)


def test_week_bounds_from_thursday_points_to_next_monday():
    mon, sun = _week_bounds(date(2026, 4, 23))  # Thu
    assert mon == date(2026, 4, 27)
    assert sun == date(2026, 5, 3)


def test_week_bounds_span_is_seven_days():
    mon, sun = _week_bounds(date(2026, 4, 26))
    assert (sun - mon).days == 6


# --------------------------------------------------------------------------- #
# blocks.weekly_plan_blocks
# --------------------------------------------------------------------------- #


def test_weekly_plan_blocks_empty_state():
    blocks = bk.weekly_plan_blocks(
        week_start=date(2026, 4, 27),
        week_end=date(2026, 5, 3),
        tasks=[],
    )
    texts = [b["text"]["text"] for b in blocks if b.get("type") == "section"]
    assert any("задач нет" in t for t in texts)


def test_weekly_plan_blocks_has_accept_and_defer_per_task(session):
    t = Task(
        title="прод",
        status=TaskStatus.backlog,
        owner_user_id="U1",
        due_date=date(2026, 4, 29),
    )
    session.add(t)
    session.flush()
    blocks = bk.weekly_plan_blocks(
        week_start=date(2026, 4, 27),
        week_end=date(2026, 5, 3),
        tasks=[t],
    )
    action_ids = [
        el["action_id"]
        for b in blocks
        if b.get("type") == "actions"
        for el in b["elements"]
    ]
    assert bk.ACTION_WEEKLY_ACCEPT in action_ids
    assert bk.ACTION_WEEKLY_DEFER in action_ids


# --------------------------------------------------------------------------- #
# send_weekly_plan service
# --------------------------------------------------------------------------- #


def _task(session, **kw):
    t = Task(title=kw.pop("title", "t"), **kw)
    session.add(t)
    session.flush()
    return t


def test_send_weekly_plan_dms_each_owner_with_backlog(session):
    today = date(2026, 4, 23)  # Thursday → week starts Monday 27
    _task(
        session,
        status=TaskStatus.backlog,
        owner_user_id="U1",
        due_date=date(2026, 4, 29),
    )
    _task(
        session,
        status=TaskStatus.backlog,
        owner_user_id="U2",
        due_date=date(2026, 4, 30),
    )
    sender = _S()
    report = send_weekly_plan(session, sender=sender, today=today)
    assert report.recipients == 2
    assert {m["channel"] for m in sender.posts} == {"U1", "U2"}


def test_send_weekly_plan_ignores_tasks_outside_week(session):
    today = date(2026, 4, 23)
    # Due next-next week — must NOT be listed.
    _task(
        session,
        status=TaskStatus.backlog,
        owner_user_id="U1",
        due_date=date(2026, 5, 15),
    )
    sender = _S()
    report = send_weekly_plan(session, sender=sender, today=today)
    # Still iterates the owner set, but picks zero tasks → skips sending.
    assert report.recipients == 0


def test_send_weekly_plan_ignores_non_backlog(session):
    today = date(2026, 4, 23)
    _task(
        session,
        status=TaskStatus.todo,
        owner_user_id="U1",
        due_date=date(2026, 4, 29),
    )
    sender = _S()
    report = send_weekly_plan(session, sender=sender, today=today)
    assert report.recipients == 0


def test_send_weekly_plan_idempotent_same_week(session):
    today = date(2026, 4, 23)
    _task(
        session,
        status=TaskStatus.backlog,
        owner_user_id="U1",
        due_date=date(2026, 4, 29),
    )
    sender = _S()
    send_weekly_plan(session, sender=sender, today=today)
    second = send_weekly_plan(session, sender=sender, today=today)
    assert second.skipped_idempotent >= 1
    assert len(sender.posts) == 1


def test_send_weekly_plan_new_week_resends(session):
    # Week 1: today=2026-04-23 (Thu) → upcoming Mon=27, Sun=05-03. Task due 04-29.
    _task(
        session,
        status=TaskStatus.backlog,
        owner_user_id="U1",
        due_date=date(2026, 4, 29),
    )
    sender = _S()
    send_weekly_plan(session, sender=sender, today=date(2026, 4, 23))
    # Week 2: today=2026-04-30 (Thu) → upcoming Mon=05-04, Sun=05-10. Task due 05-07.
    _task(
        session,
        status=TaskStatus.backlog,
        owner_user_id="U1",
        due_date=date(2026, 5, 7),
    )
    send_weekly_plan(session, sender=sender, today=date(2026, 4, 30))
    assert len(sender.posts) == 2


def test_send_weekly_plan_writes_audit_log(session):
    _task(
        session,
        status=TaskStatus.backlog,
        owner_user_id="U1",
        due_date=date(2026, 4, 29),
    )
    send_weekly_plan(session, sender=_S(), today=date(2026, 4, 23))
    log = (
        session.query(AuditLog)
        .filter(AuditLog.category == "weekly_plan")
        .one()
    )
    assert log.payload["tasks"]


# --------------------------------------------------------------------------- #
# Accept / Defer handlers
# --------------------------------------------------------------------------- #


def test_accept_transitions_backlog_to_todo(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        t = _task(
            s,
            status=TaskStatus.backlog,
            owner_user_id="U1",
            due_date=date(2026, 4, 29),
        )
        s.commit()
        tid = t.id
    sender = _S()
    handle_weekly_accept(
        body={
            "actions": [{"value": str(tid)}],
            "user": {"id": "U1"},
            "message": {"ts": "9.9"},
        },
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        t = s.get(Task, tid)
        assert t.status == TaskStatus.todo
        assert t.is_current_week is True
        assert (
            s.query(TaskStatusHistory)
            .filter_by(task_id=tid, to_status=TaskStatus.todo)
            .count()
            == 1
        )
        assert (
            s.query(AuditLog)
            .filter(AuditLog.action == "accepted")
            .count()
            == 1
        )


def test_accept_on_non_backlog_is_noop(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        t = _task(
            s,
            status=TaskStatus.in_progress,
            owner_user_id="U1",
        )
        s.commit()
        tid = t.id

    sender = _S()
    handle_weekly_accept(
        body={
            "actions": [{"value": str(tid)}],
            "user": {"id": "U1"},
            "message": {"ts": "9.9"},
        },
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        assert s.get(Task, tid).status == TaskStatus.in_progress


def test_defer_keeps_backlog_and_audits(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        t = _task(
            s,
            status=TaskStatus.backlog,
            owner_user_id="U1",
            due_date=date(2026, 4, 29),
        )
        t.is_current_week = True
        s.commit()
        tid = t.id

    sender = _S()
    handle_weekly_defer(
        body={
            "actions": [{"value": str(tid)}],
            "user": {"id": "U1"},
            "message": {"ts": "9.9"},
        },
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        t = s.get(Task, tid)
        assert t.status == TaskStatus.backlog
        assert t.is_current_week is False
        assert (
            s.query(AuditLog)
            .filter(AuditLog.action == "deferred")
            .one()
        )
