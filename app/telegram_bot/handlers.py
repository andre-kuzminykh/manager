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
    current values. The user's reply is parsed by the LLM, so they
    can write either ``key=value`` lines or free-form natural
    language ("сдвинь дедлайн на пятницу, приоритет высокий"). Raises
    ``NotAuthorised`` if the actor isn't allowed to edit."""
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
        f"Reply to this message — write naturally what to change "
        f"(e.g. `сдвинь срок на пятницу, приоритет высокий`) or use "
        f"`key=value` lines. Available fields: "
        f"{', '.join(_EDIT_KEYS)}.\n\n"
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


# Tool schema for the LLM-driven free-form parse. Each field is a
# string (or omitted) so the model is free to say "keep it" by simply
# leaving the key out — only fields the user actually mentioned end
# up in the payload.
_EDIT_TOOL_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "New task title."},
        "description": {
            "type": "string",
            "description": "New task description; empty string clears it.",
        },
        "priority": {
            "type": "string",
            "enum": ["low", "medium", "high", "urgent"],
        },
        "due": {
            "type": "string",
            "description": (
                "New due date as ISO YYYY-MM-DD. Empty string clears the due date."
            ),
        },
        "due_time": {
            "type": "string",
            "description": "New due time as HH:MM (24h). Empty clears.",
        },
        "start": {
            "type": "string",
            "description": "Start date ISO YYYY-MM-DD. Empty clears.",
        },
        "start_time": {
            "type": "string",
            "description": "Start time HH:MM (24h). Empty clears.",
        },
        "category": {
            "type": "string",
            "description": "Category label. Empty clears.",
        },
        "owner": {
            "type": "string",
            "description": (
                "New owner — Slack/Telegram user id if the user gave "
                "one explicitly, otherwise leave empty."
            ),
        },
    },
}


def _build_edit_user_prompt(*, current: dict[str, str], reply_text: str) -> str:
    """Build the user-side prompt for the Edit LLM call."""
    today = date.today().isoformat()
    cur_lines = "\n".join(f"  {k}={v}" for k, v in current.items())
    return (
        "You are editing an existing task. Read the user's reply (which "
        "may be free-form natural language in any language, or "
        "explicit `key=value` lines) and produce ONLY the fields the "
        "user wants to change.\n\n"
        f"Today is {today}.\n\n"
        "Rules:\n"
        "- Output a field only if the user actually mentioned it.\n"
        "- Resolve relative dates ('завтра', 'next Friday', 'через "
        "неделю') against today.\n"
        "- For dates emit ISO YYYY-MM-DD; for times emit HH:MM 24h.\n"
        "- To clear a field, set it to an empty string.\n"
        "- Don't invent values. If unsure, omit the key.\n\n"
        f"Current task values:\n{cur_lines}\n\n"
        f"User reply:\n{reply_text}"
    )


def parse_edit_with_llm(
    *,
    task: Task,
    reply_text: str,
    backend: Any | None,
) -> dict[str, str]:
    """LLM-driven parse of a free-form Edit reply. Falls back to the
    structured `key=value` parser when no backend is available, when
    the reply looks like explicit ``key=value`` lines, or when the LLM
    call fails.
    """
    text = (reply_text or "").strip()
    if not text:
        return {}

    # If every non-empty line is `key=value` with a known key, skip
    # the LLM — the user is being explicit.
    structured = parse_edit_payload(text)
    non_empty = [ln for ln in text.splitlines() if ln.strip()]
    if structured and len(structured) == len(non_empty):
        return structured

    if backend is None or not hasattr(backend, "call_tool"):
        # No LLM configured → best-effort structured parse only.
        return structured

    current = {
        "title": task.title or "",
        "description": task.description or "",
        "priority": task.priority.value,
        "due": task.due_date.isoformat() if task.due_date else "",
        "due_time": task.due_time.strftime("%H:%M") if task.due_time else "",
        "start": task.start_date.isoformat() if task.start_date else "",
        "start_time": task.start_time.strftime("%H:%M") if task.start_time else "",
        "category": task.category or "",
        "owner": task.owner_user_id or "",
    }
    user_prompt = _build_edit_user_prompt(current=current, reply_text=text)
    try:
        result = backend.call_tool(
            system_prompt=(
                "Extract structured task edits from the user's reply. "
                "Only include fields the user actually wants to change."
            ),
            user_prompt=user_prompt,
            tool_name="record_task_edit",
            tool_description="Record the fields the user wants to change.",
            tool_parameters=_EDIT_TOOL_PARAMS,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("telegram_edit_llm_call_failed", error=str(e))
        return structured

    if not isinstance(result, dict):
        return structured

    out: dict[str, str] = {}
    for k in _EDIT_KEYS:
        if k in result and isinstance(result[k], str):
            out[k] = result[k].strip()
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
    llm_backend: Any | None = None,
) -> Task | None:
    """Step 2 of the Edit conversation: apply the parsed payload to
    the task. The reply may be either explicit ``key=value`` lines or
    free-form natural language — when ``llm_backend`` is provided, it
    is used to extract structured field updates from the reply.
    Empty value clears the field; unknown keys are ignored.

    Returns the refreshed Task (or None if the task is missing /
    soft-deleted). Raises ``NotAuthorised`` if the actor lost the
    permission between prompt and reply.
    """
    task = session.get(Task, task_id)
    if task is None or task.deleted_at is not None:
        return None
    _ensure_can_edit(task, actor)

    if llm_backend is not None:
        payload = parse_edit_with_llm(
            task=task, reply_text=reply_text, backend=llm_backend
        )
    else:
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


# --------------------------------------------------------------------------- #
# Draft-confirm handlers (FR-CR-04-32)
# --------------------------------------------------------------------------- #


def handle_confirm_draft(
    session: Session, *, draft_id: int, actor: str
) -> tuple[Task | None, "ActionDraft | None"]:
    """Accept a pending draft: finalise it into a Task using the same
    `create_task_from_draft` helper the immediate-create path uses.

    The draft's stashed source / fallback-author / context-snapshot
    values (set by `TelegramIngestService.prepare_draft`) are popped
    off ``payload["_pending"]`` and passed through. Returns the new
    Task and the (now-confirmed) draft so the caller can replace the
    DM widgets with the regular task card.

    Idempotent: if the draft is already confirmed, return the existing
    Task (looked up via ``draft.task_id``) so a second tap on Accept
    just re-renders the card.
    """
    from app.models import ActionDraft, ActionDraftState, ProcessedTelegramMessage
    from app.persistence import create_task_from_draft

    draft = session.get(ActionDraft, draft_id)
    if draft is None:
        raise NotAuthorised("Draft not found.")
    if draft.state == ActionDraftState.confirmed and draft.task_id:
        return session.get(Task, draft.task_id), draft
    if draft.state in (ActionDraftState.ignored, ActionDraftState.expired):
        raise NotAuthorised("This draft is no longer active.")

    pending = (draft.payload or {}).get("_pending") or {}
    payload = dict(draft.payload or {})
    # Pop `_pending` so it doesn't leak into the Task — but KEEP
    # `_widgets`. The listener calls `replace_widgets_with_task_card`
    # right after this function returns, and that helper reads widget
    # locations off `draft.payload["_widgets"]`. If we cleared them
    # here, every Accept click would silently no-op the UI swap.
    payload.pop("_pending", None)
    draft.payload = payload

    source = {
        "kind": pending.get("source_kind") or "telegram",
        "conversation_id": pending.get("conversation_id"),
        "message_ts": pending.get("message_ts"),
        "thread_ts": pending.get("thread_ts"),
        "permalink": pending.get("permalink"),
    }
    task = create_task_from_draft(
        session,
        draft=draft,
        source=source,
        context_snapshot_id=pending.get("context_snapshot_id"),
        fallback_author_slack_id=pending.get("fallback_author"),
    )

    # Update the source-message bookmark so future ingest passes know
    # this message produced a real task (not just a no-action skip).
    src_chat = pending.get("source_chat_id")
    src_msg = pending.get("source_message_id")
    if src_chat is not None and src_msg is not None:
        proc = session.get(
            ProcessedTelegramMessage, (int(src_chat), int(src_msg))
        )
        if proc is not None:
            proc.task_id = task.id

    _sync_task_to_sheets(task.id)
    return task, draft


def handle_ignore_draft(
    session: Session, *, draft_id: int, actor: str
) -> "ActionDraft | None":
    """Reject a pending draft: set state=ignored. Idempotent."""
    from app.models import ActionDraft, ActionDraftState

    draft = session.get(ActionDraft, draft_id)
    if draft is None:
        raise NotAuthorised("Draft not found.")
    if draft.state == ActionDraftState.confirmed:
        # Too late — already became a task. Caller should fall back
        # to the regular Delete on the resulting task card.
        return draft
    draft.state = ActionDraftState.ignored
    session.flush()
    return draft
