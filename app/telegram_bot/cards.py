"""Helpers that post / refresh a task card in a Telegram chat.

A card is just a `sendMessage` call with text from
`build_task_card_text` and an inline keyboard from
`task_card_keyboard`. We persist the resulting `(chat_id,
message_id)` pair to `Task.card_channel` / `Task.card_ts` so later
status changes can `editMessageText` the same message in place.

The same fields are reused by the Slack flow but interpreted as a
Slack channel id and timestamp — the bot reads `task.source_kind`
to decide which client (Slack sender vs. Telegram sender) owns the
update.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import Task, TaskSourceKind
from app.services import SubscriptionService
from app.telegram_bot.keyboards import task_card_keyboard
from app.telegram_bot.sender import TelegramSender, build_task_card_text

log = get_logger(__name__)


def _keyboard_for(task: Task, viewer: str | None, *, subscribed: bool) -> dict[str, Any]:
    from app.telegram_bot.handlers import is_admin as _is_admin

    is_owner = bool(viewer) and task.owner_user_id == viewer
    return task_card_keyboard(
        task_id=task.id,
        status=task.status.value,
        is_owner=is_owner,
        is_admin=_is_admin(viewer),
        subscribed=subscribed,
    )


def post_initial_card(
    *,
    sender: TelegramSender,
    session: Session,
    task: Task,
    chat_id: int,
    reply_to_message_id: int | None,
) -> None:
    """Post the task card under the source message and remember its
    coordinates on the task row so later updates land in the same
    message via `editMessageText`."""
    if task.source_kind != TaskSourceKind.telegram or not sender.enabled:
        return
    text = build_task_card_text(task, header="Captured from this message")
    is_subscribed = SubscriptionService().is_subscribed(
        session, task=task, slack_user_id=str(task.owner_user_id or "")
    ) if task.owner_user_id else False
    kb = _keyboard_for(task, str(task.owner_user_id) if task.owner_user_id else None, subscribed=is_subscribed)

    resp = sender.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=kb,
        reply_to_message_id=reply_to_message_id,
    )
    posted_id = resp.get("message_id")
    if posted_id:
        # Reuse the Slack `card_channel` / `card_ts` columns — they
        # hold the chat id / message id for Telegram tasks.
        task.card_channel = str(chat_id)
        task.card_ts = str(posted_id)
        session.flush()


def refresh_card(
    *,
    sender: TelegramSender,
    session: Session,
    task: Task,
    viewer: str | None = None,
) -> None:
    """Edit the previously-posted card to reflect the current task
    state (status, owner, due date, available buttons). No-op when
    the task isn't a Telegram one or the sender is disabled or we
    never recorded a card."""
    if task.source_kind != TaskSourceKind.telegram or not sender.enabled:
        return
    chat_id = task.card_channel
    msg_id = task.card_ts
    if not chat_id or not msg_id:
        return

    viewer = viewer or (str(task.owner_user_id) if task.owner_user_id else None)
    is_subscribed = SubscriptionService().is_subscribed(
        session, task=task, slack_user_id=viewer or ""
    ) if viewer else False
    text = build_task_card_text(task)
    kb = _keyboard_for(task, viewer, subscribed=is_subscribed)
    try:
        sender.update_message(
            chat_id=int(chat_id),
            message_id=int(msg_id),
            text=text,
            reply_markup=kb,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("telegram_refresh_card_failed", task_id=task.id, error=str(e))


def render_tombstone(
    *,
    sender: TelegramSender,
    task: Task,
    actor: str | None,
) -> None:
    """After a soft delete, replace the card with a small tombstone
    line. Mirrors the Slack `:wastebasket: Task #N — title deleted`
    behaviour."""
    if task.source_kind != TaskSourceKind.telegram or not sender.enabled:
        return
    if not task.card_channel or not task.card_ts:
        return
    text = (
        f"🗑 Task #{task.id} — *{task.title}* deleted"
        + (f" by `{actor}`" if actor else "")
    )
    try:
        sender.update_message(
            chat_id=int(task.card_channel),
            message_id=int(task.card_ts),
            text=text,
            reply_markup={"inline_keyboard": []},
        )
    except Exception as e:  # noqa: BLE001
        log.warning("telegram_tombstone_failed", task_id=task.id, error=str(e))
