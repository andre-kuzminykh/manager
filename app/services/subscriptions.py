"""Task subscriptions (FR-CR-5)."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Task, TaskSubscription


class SubscriptionService:
    def subscribe(
        self, session: Session, *, task: Task, slack_user_id: str
    ) -> TaskSubscription:
        existing = (
            session.query(TaskSubscription)
            .filter_by(task_id=task.id, slack_user_id=slack_user_id)
            .one_or_none()
        )
        if existing is not None:
            return existing
        sub = TaskSubscription(task_id=task.id, slack_user_id=slack_user_id)
        session.add(sub)
        session.flush()
        return sub

    def unsubscribe(
        self, session: Session, *, task: Task, slack_user_id: str
    ) -> bool:
        record = (
            session.query(TaskSubscription)
            .filter_by(task_id=task.id, slack_user_id=slack_user_id)
            .one_or_none()
        )
        if record is None:
            return False
        session.delete(record)
        session.flush()
        return True

    def list_subscribers(self, session: Session, *, task: Task) -> list[str]:
        rows = (
            session.query(TaskSubscription.slack_user_id)
            .filter_by(task_id=task.id)
            .all()
        )
        return [r[0] for r in rows]

    def is_subscribed(
        self, session: Session, *, task: Task, slack_user_id: str
    ) -> bool:
        return (
            session.query(TaskSubscription)
            .filter_by(task_id=task.id, slack_user_id=slack_user_id)
            .one_or_none()
            is not None
        )
