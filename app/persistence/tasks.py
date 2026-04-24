from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models import ActionDraft, ActionDraftState, Task, TaskStatusHistory
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


def _initial_status(due: date | None, today: date | None = None) -> TaskStatus:
    """CR-01: tasks due within a week land in To Do; others sit in Backlog."""
    if due is None:
        return TaskStatus.backlog
    today = today or date.today()
    return TaskStatus.todo if due <= today + timedelta(days=7) else TaskStatus.backlog


def create_task_from_draft(
    session: Session,
    *,
    draft: ActionDraft,
    source: dict[str, Any],
    context_snapshot_id: int | None,
    fallback_author_slack_id: str | None,
) -> Task:
    """Persist a Task from a confirmed draft and mark the draft as confirmed.

    Also writes the initial TaskStatusHistory row and auto-subscribes the
    owner and source-message author (CR-01).
    """

    payload: dict[str, Any] = draft.payload or {}
    title = (payload.get("title") or "").strip()
    if not title:
        raise ValueError("Task title is required")

    owner_user_id = payload.get("owner_user_id")
    owner_display_name = payload.get("owner_display_name")
    # The upstream pipeline already populates owner_user_id = author
    # with owner_assumed=True when no explicit assignee is present
    # (see app/slack_bot/handlers/shared.py). Respect that flag so the
    # "(предположительно)" label survives into task.extra.
    owner_assumed = bool(payload.get("owner_assumed"))
    if not owner_user_id and not owner_display_name:
        # Extra safety net for code paths that bypass classify_and_persist
        # (e.g. the raw @mention fallback when the LLM timed out).
        owner_user_id = fallback_author_slack_id
        if owner_user_id:
            owner_assumed = True

    due = _coerce_due(payload.get("due_date"))
    status = _initial_status(due)

    task = Task(
        title=title,
        description=payload.get("description"),
        owner_user_id=owner_user_id,
        owner_display_name=payload.get("owner_display_name"),
        priority=_coerce_priority(payload.get("priority", "medium")),
        due_date=due,
        status=status,
        is_current_week=(status == TaskStatus.todo),
        estimated_minutes=payload.get("estimated_minutes"),
        source_conversation_id=source.get("conversation_id"),
        source_message_ts=source.get("message_ts"),
        source_thread_ts=source.get("thread_ts"),
        source_permalink=source.get("permalink"),
        context_snapshot_id=context_snapshot_id,
        created_by_slack_user_id=draft.created_by_slack_user_id or fallback_author_slack_id,
        extra={"owner_assumed": True} if owner_assumed else None,
    )
    session.add(task)
    draft.state = ActionDraftState.confirmed
    session.flush()
    # Remember the link so follow-up thread replies can edit the task
    # directly (CR-03 always-create flows).
    draft.task_id = task.id
    session.flush()

    # Initial history row (from_status = None).
    session.add(
        TaskStatusHistory(
            task_id=task.id,
            from_status=None,
            to_status=status,
            changed_by_slack_user_id=task.created_by_slack_user_id,
            reason="created",
        )
    )

    # Auto-subscribe owner and source author (de-duplicated).
    from app.services.subscriptions import SubscriptionService

    subs = SubscriptionService()
    for uid in {owner_user_id, fallback_author_slack_id} - {None, ""}:
        if uid:
            subs.subscribe(session, task=task, slack_user_id=uid)

    session.flush()
    return task


def summarize_task(task: Task) -> str:
    parts = [task.title]
    if task.due_date:
        parts.append(f"due {task.due_date.isoformat()}")
    if task.owner_display_name:
        parts.append(f"owner {task.owner_display_name}")
    return " · ".join(parts)
