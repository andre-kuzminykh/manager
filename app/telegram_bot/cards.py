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
    # FR-CR-05-58 — Fireflies-extracted tasks get TG cards too
    # (they don't have a Telegram source message, but they're
    # delivered to the operator via the same DM channel as
    # native TG captures). Slack-sourced tasks still skip — they
    # have their own `slack_bot.cards` posting path.
    if task.source_kind == TaskSourceKind.slack or not sender.enabled:
        return

    author = author_user_id or task.created_by_slack_user_id or ""
    recipients = _recipient_user_ids(task, str(author) if author else None)
    if not recipients:
        log.info("telegram_post_initial_card_no_recipients", task_id=task.id)
        return

    text = build_task_card_text(task, header="✨ New task from this message", session=session)
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
    if task.source_kind == TaskSourceKind.slack or not sender.enabled:
        return
    cards = _stored_cards(task)
    if not cards:
        return
    text = build_task_card_text(task, session=session)
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


def replace_card_for_viewer(
    *,
    sender: TelegramSender,
    session: Session,
    task: Task,
    viewer_chat_id: int,
    reply_to_message_id: int | None = None,
) -> None:
    """FR-CR-05-47 — used by the Edit-reply flow. Deletes the
    editor's stale card and posts a fresh one under their reply
    so the chat stays clean: «когда редактируешь — старая
    удаляется, только новая есть».

    Other recipients (other admins, owner if different) still get
    their card updated in place — they didn't trigger the edit and
    don't need the «replace» UX, but they DO need to see the new
    state, so they're refreshed via the same in-place mechanism
    `refresh_card` uses.
    """
    if task.source_kind == TaskSourceKind.slack or not sender.enabled:
        return
    cards = _stored_cards(task)
    text = build_task_card_text(task, session=session)
    other_cards: list[dict[str, int]] = []
    viewer_old_cards: list[dict[str, int]] = []
    for c in cards:
        if int(c["chat_id"]) == int(viewer_chat_id):
            viewer_old_cards.append(c)
        else:
            other_cards.append(c)

    # 1. Refresh OTHER recipients' cards in place.
    for c in other_cards:
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
                "telegram_replace_card_refresh_other_failed",
                task_id=task.id,
                chat_id=c["chat_id"],
                error=str(e),
            )

    # 2. Delete the viewer's stale card(s). There's normally one,
    # but a second one can exist if the viewer was on multiple
    # admin lists at create time — clean them all.
    for c in viewer_old_cards:
        try:
            sender.delete_message(
                chat_id=c["chat_id"], message_id=c["message_id"]
            )
        except Exception as e:  # noqa: BLE001
            log.info(
                "telegram_replace_card_delete_failed",
                task_id=task.id,
                chat_id=c["chat_id"],
                message_id=c["message_id"],
                error=str(e),
            )

    # 3. Post the fresh card to the viewer under their reply.
    is_subscribed = (
        SubscriptionService().is_subscribed(
            session, task=task, slack_user_id=str(viewer_chat_id)
        )
        if task.owner_user_id
        else False
    )
    kb = _keyboard_for(task, str(viewer_chat_id), subscribed=is_subscribed)
    try:
        resp = sender.send_message(
            chat_id=int(viewer_chat_id),
            text=text,
            reply_markup=kb,
            reply_to_message_id=reply_to_message_id,
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "telegram_replace_card_post_failed",
            task_id=task.id,
            chat_id=viewer_chat_id,
            error=str(e),
        )
        resp = {}

    new_msg_id = (resp or {}).get("message_id")
    new_cards = list(other_cards)
    if new_msg_id:
        new_cards.append(
            {"chat_id": int(viewer_chat_id), "message_id": int(new_msg_id)}
        )

    # 4. Persist updated card list. `card_channel`/`card_ts` keep
    # pointing at the viewer's NEW card so single-card legacy
    # readers see the up-to-date pair.
    extra = dict(task.extra or {})
    extra["telegram_cards"] = new_cards
    task.extra = extra
    if new_msg_id:
        task.card_channel = str(viewer_chat_id)
        task.card_ts = str(new_msg_id)
    session.flush()


def _resolve_actor_label(
    session: Session | None, actor_uid: str | None
) -> str | None:
    """FR-CR-05-33 — render the actor uid as a friendly label
    using the same `team_members` / `chat_members` lookup that
    `_owner_html_link` uses. The tombstone / reject lines
    previously showed «deleted by 222968032»; now they show
    «deleted by Андрей Кузьминых» when the registry has a
    matching row, falling back to `@handle` and finally the raw
    uid.
    """
    if not actor_uid:
        return None
    if session is None:
        return actor_uid
    try:
        from app.telegram_bot.sender import _resolve_owner_link_target

        _, handle, real_name = _resolve_owner_link_target(
            session, actor_uid, None
        )
    except Exception:  # noqa: BLE001
        return actor_uid
    if real_name:
        return real_name
    if handle:
        return f"@{handle}"
    return actor_uid


def render_tombstone(
    *,
    sender: TelegramSender,
    task: Task,
    actor: str | None,
    session: Session | None = None,
) -> None:
    """Replace every delivered card with a tombstone line.

    FR-CR-05-65 — same source-kind relaxation as
    `post_initial_card`: telegram-source AND fireflies-source
    tasks both go through this path. Slack tasks have their
    own `slack_bot.cards` tombstone, so we skip them here.
    """
    if task.source_kind == TaskSourceKind.slack or not sender.enabled:
        return
    cards = _stored_cards(task)
    if not cards:
        return

    actor_label = _resolve_actor_label(session, actor)
    text = (
        f"🗑 Task #{task.id} — <b>{_escape_md(task.title)}</b> — deleted"
        + (f" by {_escape_md(actor_label)}" if actor_label else "")
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


def _build_draft_widget_text(
    draft: ActionDraft, *, session: Session | None = None
) -> str:
    """FR-CR-05-13 / FR-CR-05-16 / FR-CR-05-18 / FR-CR-05-19 —
    compact HTML preview that matches the live task-card layout.

        {bullet} <a href="permalink"><b>title</b></a>
        📝 <description>
        👤 <owner-deeplink> · 📅 <due>

    Owner deeplink uses the registry-resolved tg_user_id /
    tg_handle when ``session`` is provided (FR-CR-05-19), so a
    teammate whose row carries only a `@username` (no numeric
    user_id stored on the task) still hyperlinks to a real
    profile.
    """
    from app.telegram_bot.sender import _owner_html_link, _resolve_owner_link_target

    payload = draft.payload or {}
    title = payload.get("title") or ""
    # FR-CR-05-75 — capitalize first character so draft widgets
    # match the eventual Task-card rendering (`create_task_from_
    # draft` does the same on persist). Without this the
    # operator sees lowercase «сообщить о закрытии раунда» on
    # the confirm widget and «Сообщить о закрытии раунда» on
    # the post-Accept card — inconsistent and looks unfixed.
    if title and not title[0].isupper():
        title = title[0].upper() + title[1:]
    owner_user_id = payload.get("owner_user_id") or ""
    payload_disp = payload.get("owner_display_name") or ""
    priority = payload.get("priority") or "medium"
    due = payload.get("due_date") or ""
    due_time = payload.get("due_time") or ""
    description = payload.get("description") or ""
    permalink = ((payload.get("_pending") or {}).get("permalink")) or ""

    priority_em = {
        "low": "🟢", "medium": "🟡", "high": "🟠", "urgent": "🔴",
    }.get(priority, "🟡")

    safe_title = _escape_md(str(title))
    if permalink:
        title_html = (
            f'<a href="{_escape_md(str(permalink))}">'
            f"<b>{safe_title}</b></a>"
        )
    else:
        title_html = f"<b>{safe_title}</b>"
    lines = [f"{priority_em} {title_html}"]
    if description:
        lines.append(f"📝 {_escape_md(str(description))}")
    meta: list[str] = []
    # FR-CR-05-26 — registry's `real_name` wins as the display
    # label; payload display is a fallback; numeric id is the
    # last resort. `@handle` is reserved for the link href.
    tg_id, tg_handle, registry_real = _resolve_owner_link_target(
        session,
        str(owner_user_id) if owner_user_id else None,
        str(payload_disp) if payload_disp else None,
    )
    owner_label: str | None = None
    if registry_real:
        owner_label = registry_real
    else:
        candidate = (payload_disp or owner_user_id or "").strip()
        if candidate.startswith("@") and len(candidate) > 1:
            candidate = candidate[1:]
        owner_label = candidate or None
    # FR-CR-05-26 — extract handle from `@…` display as a
    # last-resort link source.
    from app.telegram_bot.sender import _handle_from_display

    effective_handle = tg_handle or _handle_from_display(str(payload_disp))
    if owner_label:
        meta.append(
            f"👤 {_owner_html_link(str(owner_user_id) if owner_user_id else None, owner_label, tg_user_id=tg_id, tg_handle=effective_handle)}"
        )
    if due:
        # FR-CR-05-74 — same render shape as live cards: append
        # `due_time` after `due_date` when both are present.
        due_str = str(due)
        if due_time:
            due_str += f" · {due_time}"
        meta.append(f"📅 {due_str}")
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

    text = _build_draft_widget_text(draft, session=session)
    keyboard = confirm_keyboard(draft_id=draft.id)
    widgets: list[dict[str, int]] = []
    for uid in out:
        try:
            uid_int = int(uid)
        except ValueError:
            continue
        # FR-CR-05-10 — no separate forward / quote DM. The widget's
        # `description` field already carries the LLM-generated
        # context summary (1-3 sentences explaining what the task
        # is about), so the operator has everything they need in a
        # single message.
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
    text = build_task_card_text(task, session=session)
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


def replace_draft_widget_for_viewer(
    *,
    sender: TelegramSender,
    draft: ActionDraft,
    viewer_chat_id: int,
    reply_to_message_id: int | None = None,
    session: Session | None = None,
) -> None:
    """FR-CR-05-80 — used by the Edit-draft reply flow. Mirrors
    `replace_card_for_viewer` for the post-Accept Task path:

      1. Refresh every OTHER recipient's widget in place
         (admins, etc. didn't trigger the edit but need to see
         the new state).
      2. DELETE the editor's stale widget(s) via
         `delete_message`.
      3. POST a fresh widget to the editor under their reply.
      4. Persist the updated `(chat_id, message_id)` list onto
         `draft.payload["_widgets"]` so the eventual Accept's
         `replace_widgets_with_task_card` finds the NEW
         widget id and converts it to a task card too.

    Without (4) the new under-reply widget would stay alive
    after Accept, with stale buttons that no longer dispatch
    (operator regression: «нажимаю Accept после Edit, прошлая
    не исчезает, новая не реагирует»)."""
    if not sender.enabled:
        return
    widgets = _draft_widgets(draft)
    text = _build_draft_widget_text(draft, session=session)
    keyboard = confirm_keyboard(draft_id=draft.id)

    other_widgets: list[dict[str, int]] = []
    viewer_old: list[dict[str, int]] = []
    for w in widgets:
        if int(w["chat_id"]) == int(viewer_chat_id):
            viewer_old.append(w)
        else:
            other_widgets.append(w)

    # 1. Refresh other recipients in place.
    for w in other_widgets:
        try:
            sender.update_message(
                chat_id=w["chat_id"],
                message_id=w["message_id"],
                text=text,
                reply_markup=keyboard,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "telegram_replace_draft_refresh_other_failed",
                draft_id=draft.id,
                chat_id=w["chat_id"],
                error=str(e),
            )

    # 2. Delete editor's stale widget(s).
    for w in viewer_old:
        try:
            sender.delete_message(
                chat_id=w["chat_id"], message_id=w["message_id"]
            )
        except Exception as e:  # noqa: BLE001
            log.info(
                "telegram_replace_draft_delete_failed",
                draft_id=draft.id,
                chat_id=w["chat_id"],
                message_id=w["message_id"],
                error=str(e),
            )

    # 3. Post fresh widget to viewer under their reply.
    try:
        resp = sender.send_message(
            chat_id=int(viewer_chat_id),
            text=text,
            reply_markup=keyboard,
            reply_to_message_id=reply_to_message_id,
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "telegram_replace_draft_post_failed",
            draft_id=draft.id,
            chat_id=viewer_chat_id,
            error=str(e),
        )
        resp = {}

    new_msg_id = (resp or {}).get("message_id")
    new_widgets = list(other_widgets)
    if new_msg_id:
        new_widgets.append(
            {"chat_id": int(viewer_chat_id), "message_id": int(new_msg_id)}
        )

    # 4. Persist new `_widgets` list back to draft.payload so
    # Accept's `replace_widgets_with_task_card` finds the new
    # message_id.
    payload = dict(draft.payload or {})
    payload["_widgets"] = new_widgets
    draft.payload = payload
    if session is not None:
        session.flush()


def refresh_draft_widgets(
    *,
    sender: TelegramSender,
    draft: ActionDraft,
    session: Session | None = None,
) -> None:
    """Re-render every delivered confirm widget from the current
    `draft.payload`. Used after Edit-on-draft to reflect the LLM's
    field updates without re-sending the widget. ``session`` is
    threaded through to the renderer so the owner deeplink can
    look up the registry (FR-CR-05-19)."""
    if not sender.enabled:
        return
    widgets = _draft_widgets(draft)
    if not widgets:
        return
    text = _build_draft_widget_text(draft, session=session)
    keyboard = confirm_keyboard(draft_id=draft.id)
    for w in widgets:
        try:
            sender.update_message(
                chat_id=w["chat_id"],
                message_id=w["message_id"],
                text=text,
                reply_markup=keyboard,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "telegram_widget_refresh_failed",
                draft_id=draft.id,
                chat_id=w["chat_id"],
                error=str(e),
            )


def render_draft_rejected(
    *,
    sender: TelegramSender,
    draft: ActionDraft,
    actor: str | None,
    session: Session | None = None,
) -> None:
    """Replace every draft widget with a "Rejected" tombstone.

    FR-CR-05-33 — same actor-label resolution as `render_tombstone`."""
    if not sender.enabled:
        return
    widgets = _draft_widgets(draft)
    if not widgets:
        return
    title = (draft.payload or {}).get("title") or ""
    actor_label = _resolve_actor_label(session, actor)
    text = (
        f"❌ Draft #{draft.id} — <b>{_escape_md(str(title))}</b> — rejected"
        + (f" by {_escape_md(actor_label)}" if actor_label else "")
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
