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


def test_build_task_card_text_renders_status_with_space():
    """Status `in_progress` is shown to users as `in progress`."""
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
    assert "#42" in text
    assert "prepare deck" in text
    # Space, not underscore.
    assert "in progress" in text
    assert "in_progress" not in text
    assert "Andre" in text
    assert "high" in text


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
