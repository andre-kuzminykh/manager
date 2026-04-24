"""Requirement coverage: FR-CR-03-6 (Sunday weekly plan — Принять /
Позже buttons)."""
from __future__ import annotations

from app.models import AuditLog, Task
from app.models.task import TaskPriority, TaskStatus
from app.slack_bot.handlers.weekly_plan import (
    handle_weekly_accept,
    handle_weekly_defer,
)


class _Sender:
    def __init__(self):
        self.posts: list[dict] = []

    def post_message(self, **kw):
        self.posts.append(kw)
        return {"ok": True}


def _body(task_id: int, *, user: str = "U-owner"):
    return {
        "actions": [{"value": str(task_id)}],
        "user": {"id": user},
        "message": {"ts": "100.0"},
    }


def test_accept_transitions_backlog_to_todo(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        t = Task(
            title="t", status=TaskStatus.backlog, owner_user_id="U-owner",
            priority=TaskPriority.medium, is_current_week=False,
        )
        s.add(t)
        s.commit()
        tid = t.id

    sender = _Sender()
    handle_weekly_accept(body=_body(tid), sender=sender, ack=ack)
    with SessionFactory() as s:
        task = s.get(Task, tid)
        assert task.status == TaskStatus.todo
        assert task.is_current_week is True
        audit = s.query(AuditLog).filter(AuditLog.category == "weekly_plan").one()
        assert audit.action == "accepted"


def test_accept_noop_when_task_not_in_backlog(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        t = Task(title="t", status=TaskStatus.todo, owner_user_id="U-owner",
                 priority=TaskPriority.medium)
        s.add(t)
        s.commit()
        tid = t.id

    handle_weekly_accept(body=_body(tid), sender=_Sender(), ack=ack)
    with SessionFactory() as s:
        assert s.query(AuditLog).count() == 0
        # Status untouched.
        assert s.get(Task, tid).status == TaskStatus.todo


def test_accept_noop_on_unknown_task(patched_session_scope, ack):
    handle_weekly_accept(body=_body(9999), sender=_Sender(), ack=ack)


def test_accept_noop_on_missing_task_id(patched_session_scope, ack):
    handle_weekly_accept(
        body={"actions": [{"value": ""}], "user": {"id": "U"}, "message": {"ts": "1"}},
        sender=_Sender(),
        ack=ack,
    )


def test_defer_clears_current_week_flag(patched_session_scope, SessionFactory, ack):
    with SessionFactory() as s:
        t = Task(title="t", status=TaskStatus.backlog, owner_user_id="U-owner",
                 priority=TaskPriority.medium, is_current_week=True)
        s.add(t)
        s.commit()
        tid = t.id

    handle_weekly_defer(body=_body(tid), sender=_Sender(), ack=ack)
    with SessionFactory() as s:
        task = s.get(Task, tid)
        assert task.is_current_week is False
        audit = s.query(AuditLog).filter(AuditLog.category == "weekly_plan").one()
        assert audit.action == "deferred"


def test_defer_noop_on_unknown_task(patched_session_scope, ack):
    handle_weekly_defer(body=_body(9999), sender=_Sender(), ack=ack)


def test_defer_noop_on_missing_task_id(patched_session_scope, ack):
    handle_weekly_defer(
        body={"actions": [{"value": ""}], "user": {"id": "U"}, "message": {"ts": "1"}},
        sender=_Sender(),
        ack=ack,
    )


def test_accept_swallows_send_failure(
    patched_session_scope, SessionFactory, ack
):
    class _BrokenSender:
        def post_message(self, **kw):
            raise RuntimeError("slack down")

    with SessionFactory() as s:
        t = Task(title="t", status=TaskStatus.backlog, owner_user_id="U-owner",
                 priority=TaskPriority.medium)
        s.add(t)
        s.commit()
        tid = t.id
    # No exception even though post_message raises.
    handle_weekly_accept(body=_body(tid), sender=_BrokenSender(), ack=ack)
