"""FR-CR-04-26 — Telegram outbound bot.

Smoke tests for the keyboard builder, callback parser, and the
sender's no-op-when-disabled behaviour. We deliberately don't hit
the real Telegram API in tests.
"""
from __future__ import annotations

from app.models import Task, TaskPriority, TaskSourceKind, TaskStatus
from app.telegram_bot.keyboards import (
    ACTION_CONFIRM,
    ACTION_DELETE,
    ACTION_DONE,
    ACTION_EDIT,
    ACTION_IGNORE,
    ACTION_START,
    ACTION_SUBSCRIBE,
    ACTION_UNSUBSCRIBE,
    confirm_keyboard,
    parse_callback_data,
    task_card_keyboard,
)
from app.telegram_bot.sender import TelegramSender, build_task_card_text


# --------------------------------------------------------------------------- #
# Inline keyboards
# --------------------------------------------------------------------------- #


def _flat_callback_actions(kb):
    return [
        b["callback_data"].split(":", 1)[0]
        for row in kb["inline_keyboard"]
        for b in row
    ]


def test_confirm_keyboard_has_three_buttons_in_order():
    kb = confirm_keyboard(draft_id=42)
    assert _flat_callback_actions(kb) == [
        ACTION_CONFIRM,
        ACTION_EDIT,
        ACTION_IGNORE,
    ]


def test_task_card_keyboard_for_owner_in_progress_shows_done_edit_cancel_delete():
    kb = task_card_keyboard(
        task_id=1, status="in_progress", is_owner=True, is_admin=False, subscribed=False
    )
    flat = _flat_callback_actions(kb)
    assert ACTION_DONE in flat
    assert ACTION_EDIT in flat
    assert ACTION_DELETE in flat


def test_task_card_keyboard_for_bystander_shows_subscribe_only():
    kb = task_card_keyboard(
        task_id=1, status="todo", is_owner=False, is_admin=False, subscribed=False
    )
    flat = _flat_callback_actions(kb)
    assert ACTION_SUBSCRIBE in flat
    assert ACTION_DELETE not in flat
    assert ACTION_EDIT not in flat


def test_task_card_keyboard_subscribe_toggles_to_unsubscribe():
    kb = task_card_keyboard(
        task_id=1, status="todo", is_owner=False, is_admin=False, subscribed=True
    )
    flat = _flat_callback_actions(kb)
    assert ACTION_UNSUBSCRIBE in flat
    assert ACTION_SUBSCRIBE not in flat


def test_task_card_keyboard_edit_and_delete_share_a_row():
    """UX choice: ✏ Edit and 🗑 Delete must live on the same row
    (two side-by-side buttons), not stacked on separate rows."""
    kb = task_card_keyboard(
        task_id=1, status="todo", is_owner=True, is_admin=False, subscribed=False
    )
    rows = kb["inline_keyboard"]
    edit_delete_row = [
        row for row in rows
        if {b["callback_data"].split(":", 1)[0] for b in row} == {ACTION_EDIT, ACTION_DELETE}
    ]
    assert len(edit_delete_row) == 1, (
        f"expected exactly one row with both Edit and Delete; rows={rows}"
    )
    assert len(edit_delete_row[0]) == 2


def test_task_card_keyboard_start_is_owner_only():
    """FR-CR-05-08 — Start is reserved for the assignee. Even an
    admin sees Edit/Delete but not Start; bystanders see only the
    Subscribe toggle."""
    from app.telegram_bot.keyboards import (
        ACTION_DELETE,
        ACTION_EDIT,
        ACTION_START,
        ACTION_SUBSCRIBE,
    )

    # Owner sees Start.
    kb = task_card_keyboard(
        task_id=1, status="todo", is_owner=True, is_admin=False, subscribed=False
    )
    flat = _flat_callback_actions(kb)
    assert ACTION_START in flat
    assert ACTION_EDIT in flat
    assert ACTION_DELETE in flat
    assert ACTION_SUBSCRIBE not in flat

    # Admin sees Edit/Delete + Subscribe but NO Start.
    kb = task_card_keyboard(
        task_id=1, status="todo", is_owner=False, is_admin=True, subscribed=False
    )
    flat = _flat_callback_actions(kb)
    assert ACTION_START not in flat
    assert ACTION_EDIT in flat
    assert ACTION_DELETE in flat
    assert ACTION_SUBSCRIBE in flat

    # Bystander sees only the Subscribe toggle.
    kb = task_card_keyboard(
        task_id=1, status="todo", is_owner=False, is_admin=False, subscribed=False
    )
    flat = _flat_callback_actions(kb)
    assert ACTION_START not in flat
    assert ACTION_EDIT not in flat
    assert ACTION_DELETE not in flat
    assert ACTION_SUBSCRIBE in flat


def test_task_card_keyboard_does_not_show_cancel_anywhere():
    """⤺ Cancel was removed from the UI — it must not appear on
    any status / role combination."""
    from app.telegram_bot.keyboards import ACTION_CANCEL

    for status in ("backlog", "todo", "in_progress", "done"):
        for is_owner in (True, False):
            for is_admin in (True, False):
                kb = task_card_keyboard(
                    task_id=1,
                    status=status,
                    is_owner=is_owner,
                    is_admin=is_admin,
                    subscribed=False,
                )
                flat = _flat_callback_actions(kb)
                assert ACTION_CANCEL not in flat, (
                    f"unexpected Cancel for status={status} owner={is_owner} admin={is_admin}"
                )


def test_task_card_keyboard_done_status_collapses_to_delete_only():
    kb = task_card_keyboard(
        task_id=1, status="done", is_owner=True, is_admin=False, subscribed=False
    )
    flat = _flat_callback_actions(kb)
    # No transition / edit / cancel buttons on a finished task.
    assert ACTION_DONE not in flat
    assert ACTION_EDIT not in flat
    # But owner can still delete it.
    assert ACTION_DELETE in flat


# --------------------------------------------------------------------------- #
# callback_data round-trip
# --------------------------------------------------------------------------- #


def test_parse_callback_data_round_trip():
    assert parse_callback_data("confirm:7") == ("confirm", 7)
    assert parse_callback_data("done:1234567") == ("done", 1234567)


def test_parse_callback_data_rejects_garbage():
    assert parse_callback_data("nope") is None
    assert parse_callback_data("") is None
    assert parse_callback_data("confirm:not-a-number") is None


# --------------------------------------------------------------------------- #
# Card text rendering
# --------------------------------------------------------------------------- #


def test_build_task_card_text_renders_underscore_username_as_plain_html():
    """HTML parse mode renders ``andre_andreevich`` as-is — no
    Markdown italic-trigger problems, no backslash escapes."""
    t = Task(
        id=42,
        title="prep deck",
        owner_display_name="@andre_andreevich",
        priority=TaskPriority.medium,
        status=TaskStatus.todo,
        source_kind=TaskSourceKind.telegram,
    )
    text = build_task_card_text(t)
    # Underscore stays literal; @ stays literal. No backslash escapes.
    assert "@andre_andreevich" in text
    assert "\\_" not in text


def test_build_task_card_text_escapes_html_special_chars_in_title():
    t = Task(
        id=1,
        title="<urgent> task & follow-up",
        owner_user_id="U1",
        priority=TaskPriority.high,
        status=TaskStatus.in_progress,
    )
    text = build_task_card_text(t)
    # `<`, `>`, `&` are escaped to entity references.
    assert "&lt;urgent&gt;" in text
    assert "task &amp; follow-up" in text


def test_build_task_card_text_uses_minimal_layout_no_id_no_status_no_priority_word():
    """FR-CR-05-16 — task card uses the same layout as the confirm
    widget: priority emoji + bold title, description, owner +
    due, source link. No `#id`, no `status word`, no `priority
    word` — colour carries the signal."""
    t = Task(
        id=42,
        title="prepare deck",
        owner_user_id="U-andre",
        owner_display_name="Andre",
        priority=TaskPriority.high,
        status=TaskStatus.in_progress,
        source_kind=TaskSourceKind.telegram,
    )
    text = build_task_card_text(t)
    # Title in bold + priority emoji prefix.
    assert "🟠" in text
    assert "<b>prepare deck</b>" in text
    # Owner shows up.
    assert "Andre" in text
    # Removed signal: no id / status / priority word.
    assert "#42" not in text
    assert "in progress" not in text
    assert "in_progress" not in text
    assert "high" not in text
    assert "medium" not in text


def test_build_task_card_text_marks_done_with_check_emoji():
    """A finished task shows ✅ in lieu of the priority circle so
    closed work is visually distinct at a glance."""
    t = Task(
        id=1,
        title="prepare deck",
        owner_user_id="U-andre",
        owner_display_name="Andre",
        priority=TaskPriority.high,
        status=TaskStatus.done,
        source_kind=TaskSourceKind.telegram,
    )
    text = build_task_card_text(t)
    first_line = text.splitlines()[0]
    assert first_line.startswith("✅")
    assert "🟠" not in text  # priority circle suppressed for done


def test_build_task_card_text_renders_owner_as_tg_user_link():
    """FR-CR-05-16 — numeric Telegram uid → owner label is wrapped
    in a `tg://user?id=<uid>` deeplink so a tap opens a private
    chat with that person."""
    t = Task(
        id=1,
        title="x",
        owner_user_id="222968032",
        owner_display_name="Андрей Кузьминых",
        priority=TaskPriority.medium,
        status=TaskStatus.todo,
        source_kind=TaskSourceKind.telegram,
    )
    text = build_task_card_text(t)
    assert '<a href="tg://user?id=222968032">' in text
    assert "Андрей Кузьминых" in text


def test_build_task_card_text_skips_link_for_slack_uid():
    """A Slack uid (`U…`) doesn't translate to a Telegram deeplink;
    label renders as plain text."""
    t = Task(
        id=1,
        title="x",
        owner_user_id="U09SLACK",
        owner_display_name="Slack User",
        priority=TaskPriority.medium,
        status=TaskStatus.todo,
    )
    text = build_task_card_text(t)
    assert "tg://user?id=" not in text
    assert "Slack User" in text


def test_build_task_card_text_links_owner_via_at_handle_when_no_numeric_id():
    """FR-CR-05-18 — when the owner's display is `@handle` form
    but the stored owner_user_id is non-numeric (Slack-only
    teammate or unresolved row), fall back to a
    `https://t.me/<handle>` link so the operator can still tap
    through to the user's Telegram profile."""
    t = Task(
        id=1,
        title="x",
        owner_user_id="U09SLACK",
        owner_display_name="@andre_andreevich",
        priority=TaskPriority.medium,
        status=TaskStatus.todo,
        source_kind=TaskSourceKind.telegram,
    )
    text = build_task_card_text(t)
    assert '<a href="https://t.me/andre_andreevich">' in text
    assert "@andre_andreevich" in text
    # No tg://user?id= since we don't have a numeric uid.
    assert "tg://user?id=" not in text


def test_build_task_card_text_wraps_title_in_source_link():
    """FR-CR-05-18 — the title itself is the source-message
    hyperlink. No separate 🔗 line — single-tap behaviour, less
    visual noise."""
    t = Task(
        id=1,
        title="x",
        owner_user_id="111",
        priority=TaskPriority.medium,
        status=TaskStatus.todo,
        source_kind=TaskSourceKind.telegram,
        source_permalink="https://t.me/c/2061886148/2981",
    )
    text = build_task_card_text(t)
    assert (
        '<a href="https://t.me/c/2061886148/2981"><b>x</b></a>' in text
    )
    # Separate 🔗 line is gone — the link is on the title.
    assert "🔗" not in text


def test_build_task_card_text_falls_back_to_plain_bold_without_permalink():
    """When the source has no shareable URL (private DM, basic
    group), the title renders as plain `<b>title</b>` — no broken
    `<a href="">` element."""
    t = Task(
        id=1,
        title="x",
        owner_user_id="111",
        priority=TaskPriority.medium,
        status=TaskStatus.todo,
        source_permalink=None,
    )
    text = build_task_card_text(t)
    assert "<a href=" not in text.split("\n")[0]
    assert "<b>x</b>" in text


# --------------------------------------------------------------------------- #
# Sender — no-op when disabled
# --------------------------------------------------------------------------- #


def test_sender_disabled_when_token_empty():
    s = TelegramSender(token="")
    assert s.enabled is False
    # Method calls return empty dicts and don't try to hit the network.
    assert s.send_message(chat_id=1, text="hi") == {}
    assert s.update_message(chat_id=1, message_id=1, text="x") == {}
    assert s.delete_message(chat_id=1, message_id=1) == {}
    assert s.answer_callback_query(callback_query_id="x") == {}


def test_sender_enabled_when_token_present():
    """Just check the flag — we don't actually call Telegram."""
    s = TelegramSender(token="123:abc")
    assert s.enabled is True
