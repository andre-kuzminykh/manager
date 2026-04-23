from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.models import ActionDraft, ActionDraftState, Task
from app.models.task import TaskPriority, TaskStatus


def _coerce_due(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _coerce_priority(value: Any) -> TaskPriority:
    try:
        return TaskPriority(value)
    except (ValueError, TypeError):
        return TaskPriority.medium


def create_task_from_draft(
    session: Session,
    *,
    draft: ActionDraft,
    source: dict[str, Any],
    context_snapshot_id: int | None,
    fallback_author_slack_id: str | None,
) -> Task:
    """Persist a Task from a confirmed draft and mark the draft as confirmed."""

    payload: dict[str, Any] = draft.payload or {}
    title = (payload.get("title") or "").strip()
    if not title:
        raise ValueError("Task title is required")

    owner_user_id = payload.get("owner_user_id")
    if not owner_user_id:
        # Owner resolution policy fallback: author of the source message.
        owner_user_id = fallback_author_slack_id

    task = Task(
        title=title,
        description=payload.get("description"),
        owner_user_id=owner_user_id,
        owner_display_name=payload.get("owner_display_name"),
        priority=_coerce_priority(payload.get("priority", "medium")),
        due_date=_coerce_due(payload.get("due_date")),
        status=TaskStatus.open,
        source_conversation_id=source.get("conversation_id"),
        source_message_ts=source.get("message_ts"),
        source_thread_ts=source.get("thread_ts"),
        source_permalink=source.get("permalink"),
        context_snapshot_id=context_snapshot_id,
        created_by_slack_user_id=draft.created_by_slack_user_id or fallback_author_slack_id,
    )
    session.add(task)
    draft.state = ActionDraftState.confirmed
    session.flush()
    return task


def summarize_task(task: Task) -> str:
    parts = [task.title]
    if task.due_date:
        parts.append(f"due {task.due_date.isoformat()}")
    if task.owner_display_name:
        parts.append(f"owner {task.owner_display_name}")
    return " · ".join(parts)
