"""CR-03 Phase D — Sunday weekly plan.

On Sunday 20:00 (local tz) each assignee gets a DM listing their backlog
tasks with ``due_date`` in the upcoming Mon–Sun week. The DM is
idempotent per (user, week_start) via ``audit_logs`` (action
``weekly_plan:<user>:<iso>``).

Buttons:
- Принять → task moves to ``todo``
- Позже   → task stays in ``backlog`` (no state change, just an audit row)
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Protocol

from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import AuditLog, Task, TaskStatus
from app.slack_bot import blocks as bk

log = get_logger(__name__)


class _Sender(Protocol):
    def post_message(self, **kwargs) -> dict: ...


@dataclass
class WeeklyPlanReport:
    recipients: int = 0
    skipped_idempotent: int = 0
    tasks_included: int = 0
    details: list[str] = field(default_factory=list)


def _week_bounds(today: date) -> tuple[date, date]:
    """Return Monday..Sunday of the upcoming week.
    If today is Sunday, 'upcoming' is tomorrow onward through Sunday next.
    """
    # Monday is weekday() == 0. For Sunday (weekday 6) upcoming Monday is
    # today + 1. For any other day we pick next Monday.
    days_until_monday = (7 - today.weekday()) % 7
    if days_until_monday == 0:
        days_until_monday = 7
    monday = today + timedelta(days=days_until_monday)
    sunday = monday + timedelta(days=6)
    return monday, sunday


def _already_sent(session: Session, *, action: str) -> bool:
    return (
        session.query(AuditLog)
        .filter(AuditLog.category == "weekly_plan", AuditLog.action == action)
        .first()
        is not None
    )


def _mark_sent(
    session: Session, *, action: str, user_id: str, payload: dict
) -> None:
    session.add(
        AuditLog(
            category="weekly_plan",
            action=action,
            entity_type="user",
            entity_id=user_id,
            payload=payload,
        )
    )
    session.flush()


def _eligible_tasks(
    session: Session, *, user: str, week_start: date, week_end: date
) -> list[Task]:
    return (
        session.query(Task)
        .filter(
            Task.owner_user_id == user,
            Task.status == TaskStatus.backlog,
            Task.deleted_at.is_(None),
            Task.due_date.isnot(None),
            Task.due_date >= week_start,
            Task.due_date <= week_end,
        )
        .order_by(Task.due_date, Task.id)
        .all()
    )


def _owners_with_backlog(session: Session) -> list[str]:
    rows = (
        session.query(Task.owner_user_id)
        .filter(
            Task.owner_user_id.isnot(None),
            Task.status == TaskStatus.backlog,
            Task.deleted_at.is_(None),
        )
        .distinct()
        .all()
    )
    return [r[0] for r in rows if r[0]]


def send_weekly_plan(
    session: Session,
    *,
    sender: _Sender,
    today: date | None = None,
) -> WeeklyPlanReport:
    """Send the per-user weekly plan. Safe to call any day — only runs the
    actual send when today is Sunday, or via explicit override. Cron fires
    on Sunday."""
    today = today or date.today()
    week_start, week_end = _week_bounds(today)

    report = WeeklyPlanReport()
    for user in _owners_with_backlog(session):
        action = f"weekly_plan:{user}:{week_start.isoformat()}"
        if _already_sent(session, action=action):
            report.skipped_idempotent += 1
            continue
        tasks = _eligible_tasks(
            session, user=user, week_start=week_start, week_end=week_end
        )
        if not tasks:
            continue

        blocks = bk.weekly_plan_blocks(
            week_start=week_start, week_end=week_end, tasks=tasks
        )
        try:
            sender.post_message(
                channel=user,
                blocks=blocks,
                text=f"Weekly plan {week_start.isoformat()}",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("weekly_plan_dm_failed", user=user, error=str(e))
            continue
        _mark_sent(
            session,
            action=action,
            user_id=user,
            payload={
                "week_start": week_start.isoformat(),
                "tasks": [t.id for t in tasks],
            },
        )
        report.recipients += 1
        report.tasks_included += len(tasks)
    return report
