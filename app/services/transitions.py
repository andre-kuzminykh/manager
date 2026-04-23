"""Status transition rules and helper to atomically apply them with history.

CR-01 FR-CR-3 / FR-CR-4 / FR-CR-7.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Task, TaskStatus, TaskStatusHistory


class InvalidTransition(ValueError):
    """Raised when a status transition is not allowed."""


# Directed graph of allowed transitions.
ALLOWED_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.backlog: {TaskStatus.todo, TaskStatus.in_progress, TaskStatus.done},
    TaskStatus.todo: {TaskStatus.in_progress, TaskStatus.backlog, TaskStatus.done},
    TaskStatus.in_progress: {TaskStatus.review, TaskStatus.done, TaskStatus.todo},
    TaskStatus.review: {TaskStatus.done, TaskStatus.in_progress},
    TaskStatus.done: {TaskStatus.in_progress},  # reopen
}


class TransitionService:
    """Applies a status transition + writes the history row atomically."""

    def apply(
        self,
        session: Session,
        *,
        task: Task,
        new_status: TaskStatus,
        actor_slack_user_id: str | None = None,
        reason: str | None = None,
    ) -> TaskStatusHistory:
        old = task.status
        if new_status == old:
            raise InvalidTransition(f"task already in {old.value}")
        if new_status not in ALLOWED_TRANSITIONS.get(old, set()):
            raise InvalidTransition(
                f"cannot transition {old.value} -> {new_status.value}"
            )

        now = datetime.now(timezone.utc)
        task.status = new_status
        if new_status == TaskStatus.in_progress and task.started_at is None:
            task.started_at = now
        if new_status == TaskStatus.done:
            task.completed_at = now

        history = TaskStatusHistory(
            task_id=task.id,
            from_status=old,
            to_status=new_status,
            changed_by_slack_user_id=actor_slack_user_id,
            reason=reason,
            at=now,
        )
        session.add(history)
        session.flush()
        return history
