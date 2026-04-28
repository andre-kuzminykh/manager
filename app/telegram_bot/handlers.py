"""Telegram-side button handlers (FR-CR-04-28).

Mirror the Slack task-card actions so a Telegram user can drive a
task through its full lifecycle without leaving the chat. Each
handler:

  - takes a `Session`, `task_id` and the actor's Telegram user id;
  - performs an authorisation check (owner-only for destructive
    actions; bystanders can subscribe);
  - applies the state change via the existing services
    (`TransitionService`, `SubscriptionService`, soft-delete);
  - returns the refreshed `Task` so the caller can post / edit the
    Telegram card with the new state.

Edit and Mark-done-with-artifact use Telegram's reply pattern
rather than a modal — that's a planned follow-up; for MVP the Edit
button just posts a help message and Mark done transitions
without an artifact (matching the FR-CR-04-21 "both fields
optional" semantic).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import AuditLog, Task, TaskSourceKind, TaskStatus
from app.services import (
    InvalidTransition,
    SubscriptionService,
    TransitionService,
)
from app.sync.task_sync import sync_task as _sync_task_to_sheets

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Permissions
# --------------------------------------------------------------------------- #


class NotAuthorised(Exception):
    """Raised when the actor isn't the task owner or admin and the
    action requires it (e.g. Cancel, Delete, Edit)."""


def _is_owner(task: Task, actor: str | None) -> bool:
    return bool(actor) and task.owner_user_id == actor


def _ensure_can_edit(task: Task, actor: str | None) -> None:
    """Owner-only for now. Admin support comes when we wire a
    TELEGRAM_ADMIN_USER_IDS env (see roadmap)."""
    if not _is_owner(task, actor):
        raise NotAuthorised(
            "Only the task owner can do this. Ask "
            f"<@{task.owner_user_id}>."
        )


# --------------------------------------------------------------------------- #
# Lifecycle handlers
# --------------------------------------------------------------------------- #


def handle_start(session: Session, *, task_id: int, actor: str) -> Task | None:
    """*Start* button: backlog/todo → in_progress."""
    task = session.get(Task, task_id)
    if task is None or task.deleted_at is not None:
        return None
    # Anyone can claim an unowned task (mirrors Slack behaviour).
    if task.owner_user_id and task.owner_user_id != actor:
        raise NotAuthorised(
            f"Only <@{task.owner_user_id}> can start this task."
        )
    if task.owner_user_id is None:
        task.owner_user_id = actor
    try:
        TransitionService().apply(
            session,
            task=task,
            new_status=TaskStatus.in_progress,
            actor_slack_user_id=actor,
        )
    except InvalidTransition as e:
        log.info("telegram_start_invalid_transition", task_id=task_id, err=str(e))
        return task
    _sync_task_to_sheets(task_id)
    return task


def handle_done(session: Session, *, task_id: int, actor: str) -> Task | None:
    """*Mark done* button — straight transition, no artifact modal.

    The Slack flow opens an optional-artifact modal here; the
    equivalent in Telegram (a follow-up reply conversation) is
    deferred. Both artifact fields are optional anyway (FR-CR-04-21),
    so transitioning with neither set is a valid completion.
    """
    task = session.get(Task, task_id)
    if task is None or task.deleted_at is not None:
        return None
    _ensure_can_edit(task, actor)
    try:
        TransitionService().apply(
            session,
            task=task,
            new_status=TaskStatus.done,
            actor_slack_user_id=actor,
        )
    except InvalidTransition:
        return task
    _sync_task_to_sheets(task_id)
    return task


def _route_on_cancel(task: Task, *, today: date | None = None) -> TaskStatus:
    """Same routing rule as the Slack handler: due_date this week →
    todo, else → backlog."""
    today = today or date.today()
    week_end = today + timedelta(days=(6 - today.weekday()))
    if task.due_date and task.due_date <= week_end:
        return TaskStatus.todo
    return TaskStatus.backlog


def handle_cancel(session: Session, *, task_id: int, actor: str) -> Task | None:
    task = session.get(Task, task_id)
    if task is None or task.deleted_at is not None:
        return None
    _ensure_can_edit(task, actor)
    target = _route_on_cancel(task)
    if task.status == target:
        return task
    try:
        TransitionService().apply(
            session,
            task=task,
            new_status=target,
            actor_slack_user_id=actor,
            reason="cancelled",
        )
    except InvalidTransition:
        return task
    _sync_task_to_sheets(task_id)
    return task


def handle_delete(session: Session, *, task_id: int, actor: str) -> Task | None:
    """Soft-delete the task. Owner-only.

    Mirrors `handle_delete_task_submit` from the Slack flow but
    without the confirmation modal — the inline button can be
    wrapped in a "tap again to confirm" pattern in the listener
    if needed; for MVP we delete on first click.
    """
    task = session.get(Task, task_id)
    if task is None or task.deleted_at is not None:
        return None
    _ensure_can_edit(task, actor)
    task.deleted_at = datetime.now(timezone.utc)
    session.add(
        AuditLog(
            category="task",
            action="task_deleted",
            entity_type="task",
            entity_id=str(task.id),
            actor=actor,
            payload={
                "title": task.title,
                "owner_user_id": task.owner_user_id,
                "status_at_delete": task.status.value,
                "via": "telegram",
            },
        )
    )
    session.flush()
    _sync_task_to_sheets(task_id)
    return task


def handle_subscribe(
    session: Session, *, task_id: int, actor: str, subscribe: bool
) -> Task | None:
    """Subscribe/Unsubscribe — bystanders only (the owner is
    auto-subscribed at creation, the toggle would be redundant)."""
    task = session.get(Task, task_id)
    if task is None or task.deleted_at is not None:
        return None
    if _is_owner(task, actor):
        # No-op: owners can't unsubscribe from their own tasks.
        return task
    subs = SubscriptionService()
    if subscribe:
        subs.subscribe(session, task=task, slack_user_id=actor)
    else:
        subs.unsubscribe(session, task=task, slack_user_id=actor)
    return task


def handle_edit_help() -> str:
    """*Edit* button MVP: returns the help text the listener should
    post in the chat. Full edit-via-conversation is a planned
    follow-up — for now we point users at Slack or a future syntax.
    """
    return (
        "✏ *Edit task*\n"
        "Inline editing is in progress. For now, edits go through "
        "the Slack card (which lives next to this task in the same "
        "shared task DB). The Telegram-native edit flow will land "
        "in the next iteration."
    )


# --------------------------------------------------------------------------- #
# Helpers exposed for the listener / tests
# --------------------------------------------------------------------------- #


def is_telegram_task(task: Task) -> bool:
    return task.source_kind == TaskSourceKind.telegram
