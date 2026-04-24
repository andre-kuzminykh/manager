"""Requirement coverage: FR-CR-03-9 (daily reminders in source
thread), NFR-CR-03-4 (reminder deduplication per task+day).

CR-03 Phase F: daily in-thread reminders for open tasks."""
from __future__ import annotations

from datetime import date, timedelta

from app.models import AuditLog, Task, TaskStatus
from app.services import send_thread_reminders


class _S:
    def __init__(self):
        self.posts: list[dict] = []

    def post_message(self, **kw):
        self.posts.append(kw)
        return {"ok": True, "ts": "0"}


def _task(
    session,
    *,
    status,
    owner="U-owner",
    thread="thr.1",
    due=None,
    title="t",
):
    t = Task(
        title=title,
        status=status,
        owner_user_id=owner,
        source_conversation_id="C1",
        source_message_ts=thread,
        source_thread_ts=thread,
        due_date=due,
    )
    session.add(t)
    session.flush()
    return t


def test_reminder_pings_in_progress_task_in_source_thread(session):
    _task(session, status=TaskStatus.in_progress, thread="10.0", title="job")
    sender = _S()
    report = send_thread_reminders(session, sender=sender, today=date(2026, 4, 23))
    assert report.reminders == 1
    msg = sender.posts[0]
    assert msg["channel"] == "C1"
    assert msg["thread_ts"] == "10.0"
    assert "<@U-owner>" in msg["text"]
    assert "как прогресс?" in msg["text"]


def test_reminder_review_text(session):
    _task(session, status=TaskStatus.review, thread="11.0", title="r")
    sender = _S()
    send_thread_reminders(session, sender=sender, today=date(2026, 4, 23))
    assert "ревью" in sender.posts[0]["text"]


def test_reminder_todo_only_if_due_this_week(session):
    today = date(2026, 4, 23)  # Thursday
    _task(
        session,
        status=TaskStatus.todo,
        thread="12.0",
        title="this_week",
        due=date(2026, 4, 25),
    )
    _task(
        session,
        status=TaskStatus.todo,
        thread="13.0",
        title="next_week",
        due=date(2026, 5, 5),
    )
    sender = _S()
    report = send_thread_reminders(session, sender=sender, today=today)
    assert report.reminders == 1
    texts = "\n".join(m["text"] for m in sender.posts)
    assert "this_week" in texts
    assert "next_week" not in texts


def test_reminder_backlog_due_this_week(session):
    today = date(2026, 4, 23)
    _task(
        session,
        status=TaskStatus.backlog,
        thread="14.0",
        title="soonish",
        due=date(2026, 4, 26),
    )
    sender = _S()
    send_thread_reminders(session, sender=sender, today=today)
    assert "soonish" in sender.posts[0]["text"]


def test_reminder_skips_done(session):
    _task(session, status=TaskStatus.done, thread="15.0")
    sender = _S()
    report = send_thread_reminders(session, sender=sender, today=date(2026, 4, 23))
    assert report.reminders == 0


def test_reminder_skipped_when_no_thread(session):
    # Task without source_conversation_id / thread_ts
    t = Task(title="x", status=TaskStatus.in_progress, owner_user_id="U1")
    session.add(t)
    session.flush()
    sender = _S()
    report = send_thread_reminders(session, sender=sender, today=date(2026, 4, 23))
    assert report.reminders == 0
    assert report.skipped_no_thread >= 1


def test_reminder_dedup_same_day(session):
    _task(session, status=TaskStatus.in_progress, thread="16.0")
    sender = _S()
    send_thread_reminders(session, sender=sender, today=date(2026, 4, 23))
    send_thread_reminders(session, sender=sender, today=date(2026, 4, 23))
    assert len(sender.posts) == 1


def test_reminder_resent_next_day(session):
    _task(session, status=TaskStatus.in_progress, thread="17.0")
    sender = _S()
    send_thread_reminders(session, sender=sender, today=date(2026, 4, 23))
    send_thread_reminders(session, sender=sender, today=date(2026, 4, 24))
    assert len(sender.posts) == 2


def test_reminder_audit_row_per_task_day(session):
    _task(session, status=TaskStatus.in_progress, thread="18.0")
    send_thread_reminders(session, sender=_S(), today=date(2026, 4, 23))
    log = (
        session.query(AuditLog)
        .filter(AuditLog.category == "thread_reminder")
        .one()
    )
    assert log.entity_type == "task"


def test_reminder_stops_when_task_marked_done(session):
    t = _task(session, status=TaskStatus.in_progress, thread="19.0")
    sender = _S()
    send_thread_reminders(session, sender=sender, today=date(2026, 4, 23))
    # Transition to done
    t.status = TaskStatus.done
    session.flush()
    send_thread_reminders(session, sender=sender, today=date(2026, 4, 24))
    # Still only the first reminder.
    assert len(sender.posts) == 1
