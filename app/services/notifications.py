"""Subscriber broadcasts (FR-CR-5 / NFR-CR-1).

Posts DMs to every subscriber via the rate-aware sender. DM channel_id for a
Slack user is the user id itself (Slack `chat.postMessage` accepts a user id
as `channel`).
"""
from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session

from app.models import Task, TaskStatus
from app.services.subscriptions import SubscriptionService


class _Sender(Protocol):
    def post_message(self, **kwargs) -> dict: ...


class NotificationService:
    def __init__(
        self,
        *,
        sender: _Sender,
        subscriptions: SubscriptionService | None = None,
    ) -> None:
        self._sender = sender
        self._subs = subscriptions or SubscriptionService()

    # ---- public API ------------------------------------------------------

    def broadcast_status_change(
        self,
        session: Session,
        *,
        task: Task,
        from_status: TaskStatus | None,
        to_status: TaskStatus,
        actor_slack_user_id: str | None,
    ) -> int:
        text = self._status_change_text(task, from_status, to_status, actor_slack_user_id)
        return self._broadcast(session, task=task, text=text)

    def notify_deadline_approaching(
        self, session: Session, *, task: Task
    ) -> int:
        text = (
            f":alarm_clock: Task *#{task.id} {task.title}* is due "
            f"{task.due_date.isoformat() if task.due_date else 'soon'}."
        )
        return self._broadcast(session, task=task, text=text)

    def notify_overdue(self, session: Session, *, task: Task) -> int:
        text = (
            f":warning: Task *#{task.id} {task.title}* is overdue "
            f"(was due {task.due_date.isoformat() if task.due_date else '?'})."
        )
        return self._broadcast(session, task=task, text=text)

    # ---- internals -------------------------------------------------------

    def _broadcast(self, session: Session, *, task: Task, text: str) -> int:
        count = 0
        for user_id in self._subs.list_subscribers(session, task=task):
            try:
                self._sender.post_message(channel=user_id, text=text)
                count += 1
            except Exception:  # noqa: BLE001 — one bad DM must not abort the rest
                continue
        return count

    @staticmethod
    def _status_change_text(
        task: Task,
        from_status: TaskStatus | None,
        to_status: TaskStatus,
        actor: str | None,
    ) -> str:
        actor_s = f"<@{actor}>" if actor else "someone"
        prev = from_status.value if from_status else "new"
        return (
            f":arrows_counterclockwise: {actor_s} moved *#{task.id} {task.title}* "
            f"from `{prev}` to `{to_status.value}`."
        )
