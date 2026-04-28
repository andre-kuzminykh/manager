"""Inline-keyboard builders for the Telegram bot.

Telegram's button layer uses callback_data strings — there's no
``action_id``/``value`` split like Slack's Block Kit. We pack action
+ entity id into a single string with a ``:`` separator and parse it
on the way back. This is the same shape the Slack handlers use, so
porting the action graph is straightforward.

callback_data format: ``"<action>:<entity_id>"`` where action is one
of ``confirm`` / ``ignore`` / ``start`` / ``done`` / ``cancel`` /
``delete`` / ``edit`` / ``subscribe`` / ``unsubscribe``, and
entity_id is the draft id or task id (a positive integer).
"""
from __future__ import annotations

from typing import Any

ACTION_CONFIRM = "confirm"
ACTION_IGNORE = "ignore"
ACTION_START = "start"
ACTION_DONE = "done"
ACTION_CANCEL = "cancel"
ACTION_DELETE = "delete"
ACTION_EDIT = "edit"
ACTION_SUBSCRIBE = "subscribe"
ACTION_UNSUBSCRIBE = "unsubscribe"


def _btn(text: str, action: str, entity_id: int) -> dict[str, Any]:
    return {"text": text, "callback_data": f"{action}:{entity_id}"}


def _row(*buttons: dict[str, Any]) -> list[dict[str, Any]]:
    return list(buttons)


def confirm_keyboard(*, draft_id: int) -> dict[str, Any]:
    """Inline keyboard for a draft card (Accept / Edit / Reject).

    The Edit button opens a follow-up message conversation rather
    than a Telegram modal (Telegram doesn't have Slack-style modals).
    For the MVP, Edit is wired in but launches a separate flow that
    is not yet implemented; users land in their existing Slack flow
    or simply use the Telegram /edit command (planned).
    """
    return {
        "inline_keyboard": [
            _row(
                _btn("✅ Accept", ACTION_CONFIRM, draft_id),
                _btn("✏ Edit", ACTION_EDIT, draft_id),
                _btn("✖ Reject", ACTION_IGNORE, draft_id),
            )
        ]
    }


def task_card_keyboard(
    *, task_id: int, status: str, is_owner: bool, is_admin: bool, subscribed: bool
) -> dict[str, Any]:
    """Inline keyboard for a confirmed task card. Layout mirrors the
    Slack card: Start (when not started), Mark done (when in progress),
    Edit / Cancel / Delete for owner+admin, Subscribe toggle for
    bystanders.
    """
    rows: list[list[dict[str, Any]]] = []
    primary: list[dict[str, Any]] = []

    if status in ("backlog", "todo"):
        if is_owner or status == "backlog":
            primary.append(_btn("▶ Start", ACTION_START, task_id))
    elif status == "in_progress":
        primary.append(_btn("✔ Mark done", ACTION_DONE, task_id))
    if primary:
        rows.append(primary)

    secondary: list[dict[str, Any]] = []
    if status != "done" and (is_owner or is_admin):
        secondary.append(_btn("✏ Edit", ACTION_EDIT, task_id))
    if status != "backlog" and (is_owner or is_admin):
        secondary.append(_btn("⤺ Cancel", ACTION_CANCEL, task_id))
    if secondary:
        rows.append(secondary)

    sub_row: list[dict[str, Any]] = []
    if status != "done" and not is_owner:
        if subscribed:
            sub_row.append(_btn("🔕 Unsubscribe", ACTION_UNSUBSCRIBE, task_id))
        else:
            sub_row.append(_btn("🔔 Subscribe", ACTION_SUBSCRIBE, task_id))
    if sub_row:
        rows.append(sub_row)

    if is_owner or is_admin:
        rows.append([_btn("🗑 Delete", ACTION_DELETE, task_id)])

    return {"inline_keyboard": rows}


def parse_callback_data(data: str) -> tuple[str, int] | None:
    """Inverse of `_btn` — used by the inbound callback handler."""
    try:
        action, entity = data.split(":", 1)
        return action, int(entity)
    except (ValueError, AttributeError):
        return None
