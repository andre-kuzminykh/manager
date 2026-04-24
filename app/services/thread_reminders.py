"""CR-03 Phase F — daily in-thread reminders for open tasks.

For every open task with a recorded source thread, post a nudge in that
thread tagging the assignee. Dedup per (task_id, date) via
``audit_logs``.

Text per status:
- in_progress: ":raised_hand: <@owner> задача #N — как прогресс?"
- todo (due this week): ":calendar: <@owner> на этой неделе ожидаем:
  *<title>* — до <due>"
- review: ":eyes: <@owner> нужен ревью задачи #N"
- backlog (due this week): ":bookmark: <@owner> задача *#N* ждёт
  старта — до <due>"

Done / out-of-week backlog / overdue-past tasks are left alone.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol

from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import AuditLog, Task, TaskStatus

log = get_logger(__name__)


class _Sender(Protocol):
    def post_message(self, **kwargs) -> dict: ...


@dataclass
class ReminderReport:
    reminders: int = 0
    skipped_idempotent: int = 0
    skipped_no_thread: int = 0
    skipped_not_relevant: int = 0


def _already_sent(session: Session, *, action: str) -> bool:
    return (
        session.query(AuditLog)
        .filter(
            AuditLog.category == "thread_reminder", AuditLog.action == action
        )
        .first()
        is not None
    )


def _mark_sent(session: Session, *, action: str, task_id: int) -> None:
    session.add(
        AuditLog(
            category="thread_reminder",
            action=action,
            entity_type="task",
            entity_id=str(task_id),
        )
    )
    session.flush()


def _reminder_text(task: Task, *, today: date) -> str | None:
    owner = f"<@{task.owner_user_id}>" if task.owner_user_id else "команда"
    week_end = today + timedelta(days=(6 - today.weekday()))

    if task.status == TaskStatus.in_progress:
        return f":raised_hand: {owner} задача *#{task.id}* — как прогресс?"
    if task.status == TaskStatus.review:
        return f":eyes: {owner} нужен ревью задачи *#{task.id}*"
    if task.status == TaskStatus.todo:
        if task.due_date and task.due_date <= week_end:
            return (
                f":calendar: {owner} на этой неделе ожидаем: *{task.title}*"
                + (f" — до {task.due_date.isoformat()}" if task.due_date else "")
            )
        return None
    if task.status == TaskStatus.backlog:
        if task.due_date and task.due_date <= week_end:
            return (
                f":bookmark: {owner} *{task.title}* ждёт старта"
                + (f" — до {task.due_date.isoformat()}" if task.due_date else "")
            )
        return None
    return None  # done — stop pinging


def send_thread_reminders(
    session: Session,
    *,
    sender: _Sender,
    today: date | None = None,
) -> ReminderReport:
    today = today or date.today()
    report = ReminderReport()

    open_statuses = (
        TaskStatus.backlog,
        TaskStatus.todo,
        TaskStatus.in_progress,
        TaskStatus.review,
    )
    tasks = (
        session.query(Task)
        .filter(Task.status.in_(open_statuses))
        .order_by(Task.id)
        .all()
    )
    for t in tasks:
        channel = t.source_conversation_id
        thread_ts = t.source_thread_ts or t.source_message_ts
        if not channel or not thread_ts:
            report.skipped_no_thread += 1
            continue
        text = _reminder_text(t, today=today)
        if not text:
            report.skipped_not_relevant += 1
            continue
        action = f"reminder:{t.id}:{today.isoformat()}"
        if _already_sent(session, action=action):
            report.skipped_idempotent += 1
            continue
        try:
            sender.post_message(
                channel=channel,
                thread_ts=thread_ts,
                text=text,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "thread_reminder_post_failed", task_id=t.id, error=str(e)
            )
            continue
        _mark_sent(session, action=action, task_id=t.id)
        report.reminders += 1
    return report
