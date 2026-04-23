"""Daily / weekly digests + deadline reminders (FR-CR-6).

Designed to be invoked by cron / Cloud Scheduler:

    python -m ops.send_digest --type daily
    python -m ops.send_digest --type weekly
    python -m ops.send_digest --type deadlines

Idempotency (NFR-CR-2): digests are tracked in the `audit_logs` table under
category = "digest" with action = "{daily|weekly|deadlines}:{yyyy-mm-dd}".
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Protocol

from sqlalchemy.orm import Session

from app.models import AuditLog, Task, TaskStatus


class _Sender(Protocol):
    def post_message(self, **kwargs) -> dict: ...


class DigestKind(str, enum.Enum):
    daily = "daily"
    weekly = "weekly"
    deadlines = "deadlines"


@dataclass
class DigestReport:
    recipients: int = 0
    tasks_included: int = 0
    skipped_idempotent: int = 0
    details: list[str] = field(default_factory=list)


_OPEN = (TaskStatus.backlog, TaskStatus.todo, TaskStatus.in_progress, TaskStatus.review)


def _owners(session: Session) -> list[str]:
    rows = (
        session.query(Task.owner_user_id)
        .filter(Task.owner_user_id.isnot(None), Task.status.in_(_OPEN))
        .distinct()
        .all()
    )
    return [r[0] for r in rows if r[0]]


def _already_sent(session: Session, *, action: str) -> bool:
    return (
        session.query(AuditLog)
        .filter(AuditLog.category == "digest", AuditLog.action == action)
        .first()
        is not None
    )


def _mark_sent(session: Session, *, action: str, user_id: str, payload: dict) -> None:
    session.add(
        AuditLog(
            category="digest",
            action=action,
            entity_type="user",
            entity_id=user_id,
            payload=payload,
        )
    )
    # Flush so that subsequent _already_sent() checks in the same session
    # observe this row (important for NFR-CR-2 idempotency).
    session.flush()


def _fmt(tasks: list[Task]) -> str:
    if not tasks:
        return "(none)"
    return "\n".join(
        f"• *#{t.id}* {t.title} · `{t.status.value}`"
        + (f" · due {t.due_date.isoformat()}" if t.due_date else "")
        for t in tasks
    )


class DigestService:
    def __init__(self, *, sender: _Sender) -> None:
        self._sender = sender

    def send(
        self, session: Session, kind: DigestKind, *, today: date | None = None
    ) -> DigestReport:
        today = today or date.today()
        if kind == DigestKind.daily:
            return self._daily(session, today)
        if kind == DigestKind.weekly:
            return self._weekly(session, today)
        return self._deadlines(session, today)

    # ---- daily -----------------------------------------------------------

    def _daily(self, session: Session, today: date) -> DigestReport:
        report = DigestReport()
        for user in _owners(session):
            action = f"daily:{user}:{today.isoformat()}"
            if _already_sent(session, action=action):
                report.skipped_idempotent += 1
                continue

            today_tasks = self._tasks_due_on(session, user, today)
            approaching = self._tasks_due_between(
                session, user, today + timedelta(days=1), today + timedelta(days=2)
            )
            overdue = self._overdue(session, user, today)

            body = (
                f"*Your tasks for {today.isoformat()}*\n\n"
                f"*Today ({len(today_tasks)})*\n{_fmt(today_tasks)}\n\n"
                f"*Approaching ({len(approaching)})*\n{_fmt(approaching)}\n\n"
                f"*Overdue ({len(overdue)})*\n{_fmt(overdue)}"
            )
            self._sender.post_message(channel=user, text="Daily digest", blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": body}}
            ])
            _mark_sent(
                session,
                action=action,
                user_id=user,
                payload={
                    "today": len(today_tasks),
                    "approaching": len(approaching),
                    "overdue": len(overdue),
                },
            )
            report.recipients += 1
            report.tasks_included += len(today_tasks) + len(approaching) + len(overdue)
        return report

    # ---- weekly ----------------------------------------------------------

    def _weekly(self, session: Session, today: date) -> DigestReport:
        report = DigestReport()
        week_start = today - timedelta(days=today.weekday())  # Monday
        week_end = week_start + timedelta(days=6)
        for user in _owners(session):
            action = f"weekly:{user}:{week_start.isoformat()}"
            if _already_sent(session, action=action):
                report.skipped_idempotent += 1
                continue
            planned = self._tasks_due_between(session, user, week_start, week_end)
            attention = self._tasks_needing_attention(session, user, today)
            body = (
                f"*Week of {week_start.isoformat()} – {week_end.isoformat()}*\n\n"
                f"*Planned ({len(planned)})*\n{_fmt(planned)}\n\n"
                f"*Attention ({len(attention)})*\n{_fmt(attention)}"
            )
            self._sender.post_message(channel=user, text="Weekly digest", blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": body}}
            ])
            _mark_sent(
                session,
                action=action,
                user_id=user,
                payload={"planned": len(planned), "attention": len(attention)},
            )
            report.recipients += 1
            report.tasks_included += len(planned) + len(attention)
        return report

    # ---- deadlines -------------------------------------------------------

    def _deadlines(self, session: Session, today: date) -> DigestReport:
        report = DigestReport()
        soon = today + timedelta(days=2)
        tasks = (
            session.query(Task)
            .filter(
                Task.status.in_(_OPEN),
                Task.due_date.isnot(None),
                Task.due_date <= soon,
            )
            .all()
        )
        for t in tasks:
            if not t.owner_user_id:
                continue
            action = f"deadline:{t.id}:{today.isoformat()}"
            if _already_sent(session, action=action):
                report.skipped_idempotent += 1
                continue
            overdue = t.due_date < today
            label = ":warning: Overdue" if overdue else ":alarm_clock: Approaching"
            self._sender.post_message(
                channel=t.owner_user_id,
                text="Deadline reminder",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"{label}: *#{t.id} {t.title}* due {t.due_date}",
                        },
                    }
                ],
            )
            _mark_sent(
                session,
                action=action,
                user_id=t.owner_user_id,
                payload={"task_id": t.id, "overdue": overdue},
            )
            report.recipients += 1
            report.tasks_included += 1
        return report

    # ---- helpers ---------------------------------------------------------

    def _tasks_due_on(self, session: Session, user: str, d: date) -> list[Task]:
        return (
            session.query(Task)
            .filter(
                Task.owner_user_id == user,
                Task.status.in_(_OPEN),
                Task.due_date == d,
            )
            .all()
        )

    def _tasks_due_between(
        self, session: Session, user: str, a: date, b: date
    ) -> list[Task]:
        return (
            session.query(Task)
            .filter(
                Task.owner_user_id == user,
                Task.status.in_(_OPEN),
                Task.due_date.isnot(None),
                Task.due_date >= a,
                Task.due_date <= b,
            )
            .all()
        )

    def _overdue(self, session: Session, user: str, today: date) -> list[Task]:
        return (
            session.query(Task)
            .filter(
                Task.owner_user_id == user,
                Task.status.in_(_OPEN),
                Task.due_date.isnot(None),
                Task.due_date < today,
            )
            .all()
        )

    def _tasks_needing_attention(
        self, session: Session, user: str, today: date
    ) -> list[Task]:
        return (
            session.query(Task)
            .filter(
                Task.owner_user_id == user,
                Task.status.in_((TaskStatus.review, TaskStatus.in_progress)),
            )
            .all()
        )
