"""Helpers that post / refresh task cards over Telegram.

Privacy model (FR-CR-04-31): tasks captured from a Telegram chat
are NEVER posted in the source chat itself — every member would
otherwise see the card. Instead the bot DMs the card to a small
recipient set:

  - the message author (the user who wrote the task-shaped message);
  - the assignee, if the LLM resolved a different owner;
  - every Telegram admin from ``TELEGRAM_ADMIN_USER_IDS``.

So in a group only the author / owner / admins see the card; the
rest of the members see nothing. In a private chat the recipient
set collapses to the user themselves and the card lands in the same
DM — there's no behavioural difference vs. the previous design.

Each DM gets its own ``message_id``; the list lives on
``Task.extra["telegram_cards"]`` so a status change can edit every
delivered copy via ``editMessageText``. ``Task.card_channel`` /
``Task.card_ts`` keep pointing at the first card for back-compat
with the Slack-shaped fields.

Important Telegram quirk: the bot can DM only users who have
already started a private conversation with it (sent ``/start`` or
any DM). DMs to never-started users fail silently with a logged
warning — those users won't see the card. Operator should ask
team members to ``/start`` the bot once.
"""
from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import ActionDraft, Task, TaskSourceKind
from app.services import SubscriptionService
from app.telegram_bot.keyboards import confirm_keyboard, task_card_keyboard
from app.telegram_bot.sender import (
    TelegramSender,
    _escape_md,
    build_task_card_text,
)

log = get_logger(__name__)


def _is_telegram_uid(uid: str | None) -> bool:
    """Numeric (possibly leading-minus for super-groups) → TG;
    Slack uids start with U / W."""
    return bool(uid) and uid.lstrip("-").isdigit()


def _recipient_user_ids(task: Task, author_id: str | None) -> list[str]:
    """Build the deduped, ordered list of TG user ids that should
    receive the card: author → owner (if different) → every TG
    admin from env (in env order)."""
    from app.telegram_bot.handlers import admin_user_ids

    out: list[str] = []
    seen: set[str] = set()
    for uid in (
        author_id,
        task.owner_user_id,
        *sorted(admin_user_ids()),
    ):
        if not uid:
            continue
        s = str(uid)
        if not _is_telegram_uid(s):
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _keyboard_for(
    task: Task, viewer: str | None, *, subscribed: bool
) -> dict[str, Any]:
    from app.telegram_bot.handlers import is_admin as _is_admin

    is_owner = bool(viewer) and task.owner_user_id == viewer
    return task_card_keyboard(
        task_id=task.id,
        status=task.status.value,
        is_owner=is_owner,
        is_admin=_is_admin(viewer),
        subscribed=subscribed,
    )


def _stored_cards(task: Task) -> list[dict[str, int]]:
    """Read the per-recipient `(chat_id, message_id)` list from
    `task.extra`, falling back to the single-card legacy shape on
    `card_channel/card_ts` if `extra` doesn't have it."""
    cards = ((task.extra or {}).get("telegram_cards") or [])
    if cards:
        return [{"chat_id": int(c["chat_id"]), "message_id": int(c["message_id"])} for c in cards]
    if task.card_channel and task.card_ts:
        try:
            return [
                {"chat_id": int(task.card_channel), "message_id": int(task.card_ts)}
            ]
        except (TypeError, ValueError):
            return []
    return []


def post_initial_card(
    *,
    sender: TelegramSender,
    session: Session,
    task: Task,
    chat_id: int,
    reply_to_message_id: int | None,
    author_user_id: str | None = None,
) -> None:
    """DM the card to author + owner (if different) + admins.

    The `chat_id` and `reply_to_message_id` arguments are kept for
    back-compat with the previous single-message behaviour (and for
    the eventual "fallback breadcrumb in the group" mode); for the
    privacy-by-default path of FR-CR-04-31 we ignore them — every
    delivered card lives in a private chat with the bot.

    `author_user_id` is the user who wrote the task-shaped message.
    Defaults to ``task.created_by_slack_user_id`` (the field is
    overloaded — for TG tasks it holds the TG user id).
    """
    if task.source_kind != TaskSourceKind.telegram or not sender.enabled:
        return

    author = author_user_id or task.created_by_slack_user_id or ""
    recipients = _recipient_user_ids(task, str(author) if author else None)
    if not recipients:
        log.info("telegram_post_initial_card_no_recipients", task_id=task.id)
        return

    text = build_task_card_text(task, header="✨ New task from this message")
    cards: list[dict[str, int]] = []
    for uid in recipients:
        try:
            uid_int = int(uid)
        except ValueError:
            continue
        is_subscribed = (
            SubscriptionService().is_subscribed(
                session, task=task, slack_user_id=uid
            )
            if task.owner_user_id
            else False
        )
        kb = _keyboard_for(task, uid, subscribed=is_subscribed)
        resp = sender.send_message(chat_id=uid_int, text=text, reply_markup=kb)
        msg_id = resp.get("message_id")
        if msg_id:
            cards.append({"chat_id": uid_int, "message_id": int(msg_id)})
        else:
            log.info(
                "telegram_card_dm_failed",
                task_id=task.id,
                uid=uid,
                hint="recipient probably hasn't /start-ed the bot",
            )

    if not cards:
        return

    extra = dict(task.extra or {})
    extra["telegram_cards"] = cards
    task.extra = extra
    # Keep the channel-agnostic `card_channel/card_ts` pair pointing
    # at the first delivered card. Slack-side code reads them too.
    task.card_channel = str(cards[0]["chat_id"])
    task.card_ts = str(cards[0]["message_id"])
    session.flush()


def refresh_card(
    *,
    sender: TelegramSender,
    session: Session,
    task: Task,
    viewer: str | None = None,
) -> None:
    """Edit every delivered card so it reflects the current task
    state. Each recipient sees a keyboard rendered from THEIR
    perspective (owner / admin / bystander) — so the author, the
    owner, and an admin can all click the buttons that make sense
    for them.

    `viewer` is no longer special — kept for back-compat with the
    single-card path. The function iterates over every stored
    `(chat_id, message_id)` pair regardless of who triggered the
    refresh.
    """
    if task.source_kind != TaskSourceKind.telegram or not sender.enabled:
        return
    cards = _stored_cards(task)
    if not cards:
        return
    text = build_task_card_text(task)
    for c in cards:
        recipient = str(c["chat_id"])
        is_subscribed = (
            SubscriptionService().is_subscribed(
                session, task=task, slack_user_id=recipient
            )
            if task.owner_user_id
            else False
        )
        kb = _keyboard_for(task, recipient, subscribed=is_subscribed)
        try:
            sender.update_message(
                chat_id=c["chat_id"],
                message_id=c["message_id"],
                text=text,
                reply_markup=kb,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "telegram_refresh_card_failed",
                task_id=task.id,
                chat_id=c["chat_id"],
                error=str(e),
            )


def render_tombstone(
    *,
    sender: TelegramSender,
    task: Task,
    actor: str | None,
) -> None:
    """Replace every delivered card with a tombstone line."""
    if task.source_kind != TaskSourceKind.telegram or not sender.enabled:
        return
    cards = _stored_cards(task)
    if not cards:
        return

    text = (
        f"🗑 Task #{task.id} — <b>{_escape_md(task.title)}</b> — deleted"
        + (f" by <code>{_escape_md(actor)}</code>" if actor else "")
    )
    for c in cards:
        try:
            sender.update_message(
                chat_id=c["chat_id"],
                message_id=c["message_id"],
                text=text,
                reply_markup={"inline_keyboard": []},
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "telegram_tombstone_failed",
                task_id=task.id,
                chat_id=c["chat_id"],
                error=str(e),
            )


# --------------------------------------------------------------------------- #
# Draft-confirm widgets (FR-CR-04-32)
# --------------------------------------------------------------------------- #
#
# When a task-shaped message lands in a group / supergroup / channel
# we don't materialise a Task immediately. Instead we DM each
# recipient (author + admins) with a forward of the original message
# plus a "Create this task?" widget carrying ✅ / ✏ / ✖ buttons.
# Only on Accept does the draft get finalised into a Task.


def _build_draft_widget_text(draft: ActionDraft) -> str:
    """Render the draft as a compact HTML preview for the widget."""
    payload = draft.payload or {}
    title = payload.get("title") or ""
    owner_disp = payload.get("owner_display_name") or payload.get("owner_user_id") or ""
    priority = payload.get("priority") or "medium"
    due = payload.get("due_date") or ""
    description = payload.get("description") or ""

    priority_em = {
        "low": "🟢", "medium": "🟡", "high": "🟠", "urgent": "🔴",
    }.get(priority, "🟡")

    lines = [f"📥 <b>Create this task?</b>"]
    lines.append(f"📌 {_escape_md(str(title))}")
    if description:
        lines.append(f"📝 {_escape_md(str(description))}")
    meta: list[str] = []
    if owner_disp:
        meta.append(f"👤 {_escape_md(str(owner_disp))}")
    meta.append(f"{priority_em} {priority}")
    if due:
        meta.append(f"📅 {due}")
    if meta:
        lines.append(" · ".join(meta))
    return "\n".join(lines)


def _draft_widgets(draft: ActionDraft) -> list[dict[str, int]]:
    """Read the per-recipient `(chat_id, message_id)` widget locations
    from `draft.payload["_widgets"]`."""
    widgets = ((draft.payload or {}).get("_widgets") or [])
    return [
        {"chat_id": int(w["chat_id"]), "message_id": int(w["message_id"])}
        for w in widgets
    ]


def post_draft_confirmation(
    *,
    sender: TelegramSender,
    session: Session,
    draft: ActionDraft,
    source_chat_id: int,
    source_message_id: int,
    author_user_id: str | None,
    owner_user_id: str | None,
) -> None:
    """DM the source forward + a confirm widget to the author + admins.

    Forwards the original message first (so the recipient sees the
    sender attribution), then sends the widget as a follow-up. Each
    delivered widget's `(chat_id, message_id)` is stashed on
    ``draft.payload["_widgets"]`` so we can edit them all when the
    user clicks Accept / Reject.
    """
    if not sender.enabled:
        return
    from app.telegram_bot.handlers import admin_user_ids

    out: list[str] = []
    seen: set[str] = set()
    for uid in (author_user_id, owner_user_id, *sorted(admin_user_ids())):
        if not uid:
            continue
        s = str(uid)
        if not s.lstrip("-").isdigit():
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)

    if not out:
        log.info("telegram_draft_no_recipients", draft_id=draft.id)
        return

    text = _build_draft_widget_text(draft)
    keyboard = confirm_keyboard(draft_id=draft.id)
    widgets: list[dict[str, int]] = []
    for uid in out:
        try:
            uid_int = int(uid)
        except ValueError:
            continue
        # Best-effort forward of the original message — fails silently
        # for users who haven't /start-ed the bot yet, same as task
        # cards. The widget itself is what carries the buttons.
        try:
            sender.forward_message(
                chat_id=uid_int,
                from_chat_id=source_chat_id,
                message_id=source_message_id,
            )
        except Exception as e:  # noqa: BLE001
            log.info(
                "telegram_draft_forward_failed",
                draft_id=draft.id,
                uid=uid,
                error=str(e),
            )
        resp = sender.send_message(
            chat_id=uid_int, text=text, reply_markup=keyboard
        )
        msg_id = resp.get("message_id")
        if msg_id:
            widgets.append({"chat_id": uid_int, "message_id": int(msg_id)})
        else:
            log.info(
                "telegram_draft_widget_dm_failed",
                draft_id=draft.id,
                uid=uid,
                hint="recipient probably hasn't /start-ed the bot",
            )

    if not widgets:
        return

    payload = dict(draft.payload or {})
    payload["_widgets"] = widgets
    draft.payload = payload
    draft.card_channel = str(widgets[0]["chat_id"])
    draft.card_ts = str(widgets[0]["message_id"])
    session.flush()


def replace_widgets_with_task_card(
    *,
    sender: TelegramSender,
    session: Session,
    draft: ActionDraft,
    task: Task,
) -> None:
    """After Accept: edit each draft widget into the regular task
    card. Also copies the widget locations onto the new Task so the
    follow-up Edit / Delete actions edit the same DMs in place."""
    if not sender.enabled:
        return
    widgets = _draft_widgets(draft)
    if not widgets:
        return
    text = build_task_card_text(task)
    cards: list[dict[str, int]] = []
    for w in widgets:
        recipient = str(w["chat_id"])
        is_subscribed = (
            SubscriptionService().is_subscribed(
                session, task=task, slack_user_id=recipient
            )
            if task.owner_user_id
            else False
        )
        kb = _keyboard_for(task, recipient, subscribed=is_subscribed)
        try:
            sender.update_message(
                chat_id=w["chat_id"],
                message_id=w["message_id"],
                text=text,
                reply_markup=kb,
            )
            cards.append({"chat_id": w["chat_id"], "message_id": w["message_id"]})
        except Exception as e:  # noqa: BLE001
            log.warning(
                "telegram_widget_replace_failed",
                draft_id=draft.id,
                task_id=task.id,
                chat_id=w["chat_id"],
                error=str(e),
            )

    if not cards:
        return
    extra = dict(task.extra or {})
    extra["telegram_cards"] = cards
    task.extra = extra
    task.card_channel = str(cards[0]["chat_id"])
    task.card_ts = str(cards[0]["message_id"])
    session.flush()


def render_draft_rejected(
    *,
    sender: TelegramSender,
    draft: ActionDraft,
    actor: str | None,
) -> None:
    """Replace every draft widget with a "Rejected" tombstone."""
    if not sender.enabled:
        return
    widgets = _draft_widgets(draft)
    if not widgets:
        return
    title = (draft.payload or {}).get("title") or ""
    text = (
        f"❌ Draft #{draft.id} — <b>{_escape_md(str(title))}</b> — rejected"
        + (f" by <code>{_escape_md(actor)}</code>" if actor else "")
    )
    for w in widgets:
        try:
            sender.update_message(
                chat_id=w["chat_id"],
                message_id=w["message_id"],
                text=text,
                reply_markup={"inline_keyboard": []},
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "telegram_draft_reject_render_failed",
                draft_id=draft.id,
                chat_id=w["chat_id"],
                error=str(e),
            )
