"""Telegram-side button handlers (FR-CR-04-28 / FR-CR-04-29).

Mirror the Slack task-card actions so a Telegram user can drive a
task through its full lifecycle without leaving the chat. Each
handler:

  - takes a `Session`, `task_id` and the actor's Telegram user id;
  - performs an authorisation check (owner / admin for destructive
    actions; bystanders can subscribe);
  - applies the state change via the existing services
    (`TransitionService`, `SubscriptionService`, soft-delete);
  - returns the refreshed `Task` so the caller can post / edit the
    Telegram card with the new state.

Two flows use Telegram's reply pattern in lieu of a modal:

- **Mark done with artifact** — the bot posts a prompt; the user's
  reply (text or `/skip`) is parsed by `apply_done_artifact_reply`.
- **Edit** — the bot posts a help message; the user's reply with
  ``key=value`` lines is parsed by `apply_edit_reply`.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.logging_setup import get_logger
from app.models import (
    AuditLog,
    Task,
    TaskPriority,
    TaskSourceKind,
    TaskStatus,
)
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


def admin_user_ids() -> set[str]:
    """Read TELEGRAM_ADMIN_USER_IDS env into a set of strings."""
    raw = (get_settings().telegram_admin_user_ids or "").strip()
    if not raw:
        return set()
    return {p.strip() for p in raw.split(",") if p.strip()}


def is_admin(user_id: str | None) -> bool:
    if not user_id:
        return False
    return user_id in admin_user_ids()


class NotAuthorised(Exception):
    """Raised when the actor isn't the task owner or an admin and
    the action requires it (e.g. Cancel, Delete, Edit)."""


def _is_owner(task: Task, actor: str | None) -> bool:
    return bool(actor) and task.owner_user_id == actor


def _ensure_can_edit(task: Task, actor: str | None) -> None:
    """Owner or TG admin."""
    if _is_owner(task, actor):
        return
    if is_admin(actor):
        return
    raise NotAuthorised(
        "Only the task owner or an admin can do this. Ask "
        f"<@{task.owner_user_id}>." if task.owner_user_id else "Only an admin can do this."
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
    """*Mark done* button — direct transition without artifact.

    The full FR-CR-04-29 flow opens a follow-up "reply with link or
    note (or /skip)" conversation via `prompt_done` +
    `apply_done_artifact_reply`. This direct entry is kept for
    callers that want the no-artifact path explicitly (tests,
    legacy in-memory paths). The Slack-equivalent fields are both
    optional anyway (FR-CR-04-21).
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


# --------------------------------------------------------------------------- #
# Mark done — reply-conversation flow (FR-CR-04-29)
# --------------------------------------------------------------------------- #


def prompt_done(
    session: Session, *, task_id: int, actor: str
) -> tuple[Task, str]:
    """Step 1 of the Mark-done conversation: return the prompt text
    the listener should post in the chat. Raises ``NotAuthorised``
    when the actor isn't allowed to complete the task."""
    task = session.get(Task, task_id)
    if task is None or task.deleted_at is not None:
        raise NotAuthorised("Task not found or already deleted.")
    _ensure_can_edit(task, actor)
    text = (
        f"✔ *Mark done — task #{task.id}*\n"
        f"Optional: reply to this message with a link or a short note "
        f"about the result.\n"
        f"Or reply `/skip` to complete without an artifact."
    )
    return task, text


def apply_done_artifact_reply(
    session: Session,
    *,
    task_id: int,
    actor: str,
    reply_text: str,
) -> Task | None:
    """Step 2 of the Mark-done conversation: parse the user's
    reply, persist the artifact (URL → kind=url, anything else →
    kind=text), and transition the task to done."""
    task = session.get(Task, task_id)
    if task is None or task.deleted_at is not None:
        return None
    _ensure_can_edit(task, actor)

    text = (reply_text or "").strip()
    if text and text != "/skip":
        if re.match(r"^https?://", text):
            task.completion_artifact = text
            task.completion_artifact_kind = "url"
        else:
            task.completion_artifact = text
            task.completion_artifact_kind = "text"

    try:
        TransitionService().apply(
            session,
            task=task,
            new_status=TaskStatus.done,
            actor_slack_user_id=actor,
        )
    except InvalidTransition:
        # Already done — keep the artifact we just stored.
        log.info("telegram_done_already_done", task_id=task_id)
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
    """Legacy stub kept for back-compat. Use `prompt_edit` for the
    full reply-conversation flow."""
    return (
        "✏ Edit task — use the inline reply flow now (see prompt_edit)."
    )


# --------------------------------------------------------------------------- #
# Edit — reply-conversation flow (FR-CR-04-29)
# --------------------------------------------------------------------------- #


_EDIT_KEYS = (
    "title",
    "description",
    "priority",
    "due",
    "due_time",
    "start",
    "start_time",
    "category",
    "owner",
)


def prompt_edit(
    session: Session, *, task_id: int, actor: str
) -> tuple[Task, str]:
    """Step 1 of the Edit conversation: return a prompt with the
    current values + format reminder. Raises ``NotAuthorised`` if
    the actor isn't allowed to edit."""
    task = session.get(Task, task_id)
    if task is None or task.deleted_at is not None:
        raise NotAuthorised("Task not found or already deleted.")
    _ensure_can_edit(task, actor)

    cur = (
        f"title={task.title}\n"
        f"description={task.description or ''}\n"
        f"priority={task.priority.value}\n"
        f"due={task.due_date.isoformat() if task.due_date else ''}\n"
        f"due_time={task.due_time.strftime('%H:%M') if task.due_time else ''}\n"
        f"start={task.start_date.isoformat() if task.start_date else ''}\n"
        f"start_time={task.start_time.strftime('%H:%M') if task.start_time else ''}\n"
        f"category={task.category or ''}\n"
        f"owner={task.owner_user_id or ''}"
    )
    text = (
        f"✏ *Edit task #{task.id}*\n"
        f"Reply to this message with `key=value` lines for the "
        f"fields you want to change. Empty value clears the field.\n\n"
        f"Available keys: {', '.join(_EDIT_KEYS)}.\n\n"
        f"Current values:\n```\n{cur}\n```"
    )
    return task, text


def parse_edit_payload(text: str) -> dict[str, str]:
    """Parse a multi-line ``key=value`` reply into a dict, dropping
    unknown keys."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().lower()
        if k in _EDIT_KEYS:
            out[k] = v.strip()
    return out


def _parse_date_or_none(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _parse_time_or_none(s: str | None) -> time | None:
    if not s:
        return None
    try:
        hh, mm = s.split(":")[:2]
        return time(int(hh), int(mm))
    except (ValueError, IndexError):
        return None


def apply_edit_reply(
    session: Session,
    *,
    task_id: int,
    actor: str,
    reply_text: str,
) -> Task | None:
    """Step 2 of the Edit conversation: apply the parsed
    ``key=value`` payload to the task. Empty value clears the
    field; unknown keys are ignored.

    Returns the refreshed Task (or None if the task is missing /
    soft-deleted). Raises ``NotAuthorised`` if the actor lost the
    permission between prompt and reply.
    """
    task = session.get(Task, task_id)
    if task is None or task.deleted_at is not None:
        return None
    _ensure_can_edit(task, actor)

    payload = parse_edit_payload(reply_text)
    if not payload:
        return task

    if "title" in payload and payload["title"]:
        # Empty title isn't allowed — keep the old one in that case.
        task.title = payload["title"]
    if "description" in payload:
        task.description = payload["description"] or None
    if "priority" in payload:
        try:
            task.priority = TaskPriority(payload["priority"])
        except ValueError:
            pass  # invalid value → leave unchanged
    if "due" in payload:
        task.due_date = _parse_date_or_none(payload["due"]) if payload["due"] else None
    if "due_time" in payload:
        task.due_time = _parse_time_or_none(payload["due_time"]) if payload["due_time"] else None
    if "start" in payload:
        task.start_date = _parse_date_or_none(payload["start"]) if payload["start"] else None
    if "start_time" in payload:
        task.start_time = _parse_time_or_none(payload["start_time"]) if payload["start_time"] else None
    if "category" in payload:
        task.category = payload["category"] or None
    if "owner" in payload:
        task.owner_user_id = payload["owner"] or None

    # Drop the "owner_assumed" flag — once a human has explicitly
    # edited the task, we no longer hedge the owner label.
    if task.extra and task.extra.get("owner_assumed"):
        extra = dict(task.extra)
        extra.pop("owner_assumed", None)
        task.extra = extra or None

    session.flush()
    _sync_task_to_sheets(task_id)
    return task


# --------------------------------------------------------------------------- #
# Helpers exposed for the listener / tests
# --------------------------------------------------------------------------- #


def is_telegram_task(task: Task) -> bool:
    return task.source_kind == TaskSourceKind.telegram
