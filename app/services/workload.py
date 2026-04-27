"""Workload-aware deadline proposal (FR-CR-2).

Heuristic:
- For a given owner, sum `estimated_minutes` of open tasks
  (status in {backlog, todo, in_progress}).
- Given a daily minute budget (default 6h = 360 min), compute how many
  *business days* (Mon-Fri) are needed to clear the queue + the new task.
- Propose a due date that lands on the first business day with enough slack.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.models import Task, TaskStatus

_OPEN_STATUSES = (TaskStatus.backlog, TaskStatus.todo, TaskStatus.in_progress)


@dataclass
class WorkloadProposal:
    due_date: date
    backlog_minutes: int
    new_task_minutes: int
    busy_business_days: int


def _next_business_day(d: date) -> date:
    d = d + timedelta(days=1)
    while d.weekday() >= 5:
        d = d + timedelta(days=1)
    return d


def _business_days_for_minutes(total_minutes: int, per_day: int) -> int:
    if total_minutes <= 0 or per_day <= 0:
        return 0
    days, rem = divmod(total_minutes, per_day)
    return days + (1 if rem else 0)


class WorkloadEstimator:
    def __init__(
        self,
        *,
        minutes_per_day: int = 360,
        default_task_minutes: int = 120,
    ) -> None:
        self._minutes_per_day = minutes_per_day
        self._default_task_minutes = default_task_minutes

    def owner_backlog_minutes(self, session: Session, owner_user_id: str) -> int:
        if not owner_user_id:
            return 0
        total = 0
        tasks = (
            session.query(Task)
            .filter(
                Task.owner_user_id == owner_user_id,
                Task.status.in_(_OPEN_STATUSES),
                Task.deleted_at.is_(None),
            )
            .all()
        )
        for t in tasks:
            total += t.estimated_minutes or self._default_task_minutes
        return total

    def propose_due_date(
        self,
        session: Session,
        *,
        owner_user_id: str | None,
        estimated_minutes: int | None,
        today: date | None = None,
    ) -> WorkloadProposal:
        today = today or date.today()
        new_minutes = estimated_minutes or self._default_task_minutes
        backlog = self.owner_backlog_minutes(session, owner_user_id or "")

        days_needed = _business_days_for_minutes(
            backlog + new_minutes, self._minutes_per_day
        )

        d = today
        while d.weekday() >= 5:
            d = _next_business_day(d)
        for _ in range(max(days_needed, 1) - 1):
            d = _next_business_day(d)

        return WorkloadProposal(
            due_date=d,
            backlog_minutes=backlog,
            new_task_minutes=new_minutes,
            busy_business_days=days_needed,
        )
