"""FR-CR-05-07 — Telegram chat-members registry.

The live listener self-populates a per-chat membership table so the
classifier can resolve mentions like «Валя сделай X» to a real
numeric user_id, and the bot can DM the assignee directly when
they've /started the bot.
"""
from __future__ import annotations

from app.models import TelegramChatMember
from app.services.telegram_members import (
    list_members_for_chat,
    members_as_known_employees,
    upsert_member,
)


def test_upsert_member_inserts_new_row(session):
    upsert_member(
        session,
        chat_id=-100,
        user_id=42,
        username="petya",
        first_name="Petya",
        last_name="Pupkin",
    )
    session.flush()
    out = session.get(TelegramChatMember, (-100, 42))
    assert out is not None
    assert out.username == "petya"
    assert out.first_name == "Petya"
    assert out.last_name == "Pupkin"
    assert out.has_started_bot is False


def test_upsert_member_idempotent(session):
    """Two calls with the same key produce a single row, with the
    later call's profile fields winning when they're non-null."""
    upsert_member(session, chat_id=-100, user_id=42, username="petya")
    upsert_member(
        session, chat_id=-100, user_id=42,
        first_name="Petya", last_name="Pupkin",
    )
    session.flush()
    rows = list_members_for_chat(session, -100)
    assert len(rows) == 1
    assert rows[0].username == "petya"     # preserved from first call
    assert rows[0].first_name == "Petya"   # set by second
    assert rows[0].last_name == "Pupkin"


def test_upsert_member_has_started_bot_is_sticky(session):
    """Once True, never flipped back to False — the user has /started
    the bot at some point and stays DM-able."""
    upsert_member(session, chat_id=42, user_id=42, has_started_bot=True)
    upsert_member(session, chat_id=42, user_id=42, has_started_bot=False)
    session.flush()
    out = session.get(TelegramChatMember, (42, 42))
    assert out.has_started_bot is True


def test_members_as_known_employees_shape(session):
    upsert_member(
        session,
        chat_id=-1001,
        user_id=222968032,
        username="andre_andreevich",
        first_name="Andre",
        last_name="Kuzminykh",
    )
    upsert_member(
        session,
        chat_id=-1001,
        user_id=412243973,
        first_name="Petya",  # no username, only first name
    )
    upsert_member(
        session,
        chat_id=-1001,
        user_id=999,
        # No name fields at all — fall back to numeric id as display
    )
    session.flush()

    out = members_as_known_employees(session, chat_id=-1001)
    out_by_id = {e["slack_user_id"]: e for e in out}
    assert "222968032" in out_by_id
    assert out_by_id["222968032"]["display_name"] == "@andre_andreevich"
    assert out_by_id["222968032"]["real_name"] == "Andre Kuzminykh"
    assert out_by_id["412243973"]["display_name"] == "Petya"
    assert out_by_id["999"]["display_name"] == "999"


def test_members_as_known_employees_isolates_chat(session):
    """A user only registered in one chat shouldn't appear in
    another's known_employees list — owner detection is per-chat."""
    upsert_member(session, chat_id=-1001, user_id=42, username="petya")
    upsert_member(session, chat_id=-2002, user_id=99, username="vasya")
    session.flush()

    chat1 = members_as_known_employees(session, chat_id=-1001)
    chat2 = members_as_known_employees(session, chat_id=-2002)
    assert {e["slack_user_id"] for e in chat1} == {"42"}
    assert {e["slack_user_id"] for e in chat2} == {"99"}
