"""Handlers for the Sunday weekly-plan buttons (CR-03 FR-CR-03-6)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from slack_bolt import Ack

from app.db import session_scope
from app.logging_setup import get_logger
from app.models import AuditLog, Task, TaskStatus, TaskStatusHistory

log = get_logger(__name__)


def _task_id(body: dict[str, Any]) -> int | None:
    try:
        return int((body.get("actions") or [{}])[0].get("value") or 0) or None
    except (TypeError, ValueError):
        return None


def _actor(body: dict[str, Any]) -> str | None:
    return (body.get("user") or {}).get("id")


def handle_weekly_accept(*, body: dict[str, Any], sender, ack: Ack) -> None:
    ack()
    task_id = _task_id(body)
    actor = _actor(body)
    if task_id is None:
        return
    with session_scope() as session:
        task = session.get(Task, task_id)
        if task is None:
            return
        if task.status != TaskStatus.backlog:
            return
        now = datetime.now(timezone.utc)
        old = task.status
        task.status = TaskStatus.todo
        task.is_current_week = True
        session.add(
            TaskStatusHistory(
                task_id=task.id,
                from_status=old,
                to_status=TaskStatus.todo,
                changed_by_slack_user_id=actor,
                reason="weekly_plan_accept",
                at=now,
            )
        )
        session.add(
            AuditLog(
                category="weekly_plan",
                action="accepted",
                entity_type="task",
                entity_id=str(task.id),
                actor=actor,
            )
        )
    try:
        sender.post_message(
            channel=actor,
            text=f":white_check_mark: Accepted: task *#{task_id}* for this week.",
            thread_ts=(body.get("message") or {}).get("ts"),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("weekly_accept_reply_failed", error=str(e))


def handle_weekly_defer(*, body: dict[str, Any], sender, ack: Ack) -> None:
    ack()
    task_id = _task_id(body)
    actor = _actor(body)
    if task_id is None:
        return
    with session_scope() as session:
        task = session.get(Task, task_id)
        if task is None:
            return
        task.is_current_week = False
        session.add(
            AuditLog(
                category="weekly_plan",
                action="deferred",
                entity_type="task",
                entity_id=str(task.id),
                actor=actor,
            )
        )
    try:
        sender.post_message(
            channel=actor,
            text=f":hourglass_flowing_sand: Deferred: task *#{task_id}*.",
            thread_ts=(body.get("message") or {}).get("ts"),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("weekly_defer_reply_failed", error=str(e))
