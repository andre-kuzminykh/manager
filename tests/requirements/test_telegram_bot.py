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


def test_build_task_card_text_includes_title_status_owner_priority_due():
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
    assert "in_progress" in text
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
