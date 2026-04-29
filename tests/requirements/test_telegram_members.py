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


def test_upsert_member_enriches_sparse_team_members_row(session):
    """FR-CR-05-21 — when the listener observes a message from a
    user who already has a `team_members` row but the row's TG
    fields are blank (auto-seeded sparse, or operator hasn't
    filled them yet), the observation populates the missing
    fields: `telegram_username` and `real_name`. Operator-edited
    values are NEVER overwritten."""
    from app.models import TeamMember

    session.add(
        TeamMember(
            telegram_user_id=97239970,
            telegram_username=None,
            real_name=None,
            active=True,
        )
    )
    session.flush()

    upsert_member(
        session,
        chat_id=-100,
        user_id=97239970,
        username="artem_sokolov",
        first_name="Артем",
        last_name="Соколов",
    )
    session.flush()

    row = (
        session.query(TeamMember)
        .filter(TeamMember.telegram_user_id == 97239970)
        .first()
    )
    assert row.telegram_username == "artem_sokolov"
    assert row.real_name == "Артем Соколов"


def test_upsert_member_does_not_overwrite_operator_edits(session):
    """Operator-edited values (a non-empty `real_name` or
    `telegram_username`) MUST be preserved. Auto-enrich fills
    BLANK fields only."""
    from app.models import TeamMember

    session.add(
        TeamMember(
            telegram_user_id=97239970,
            telegram_username="custom_handle",
            real_name="Артем Соколов - CEO",
            active=True,
        )
    )
    session.flush()

    upsert_member(
        session,
        chat_id=-100,
        user_id=97239970,
        username="artem_sokolov",   # would clobber — must NOT
        first_name="Артем",
        last_name="Соколов",
    )
    session.flush()

    row = (
        session.query(TeamMember)
        .filter(TeamMember.telegram_user_id == 97239970)
        .first()
    )
    assert row.telegram_username == "custom_handle"
    assert row.real_name == "Артем Соколов - CEO"


def test_upsert_member_creates_team_row_for_new_user(session):
    """FR-CR-05-27 — when the listener observes a brand-new user
    (no existing team_members row), `upsert_member` AUTO-CREATES
    one with whatever fields the observation provides. New
    teammates appearing in any chat the bot is in show up in the
    Team registry without manual seeding."""
    from app.models import TeamMember

    upsert_member(
        session,
        chat_id=-100,
        user_id=99999999,
        username="brand_new",
        first_name="Newbie",
        last_name="Smith",
    )
    session.flush()
    rows = (
        session.query(TeamMember)
        .filter(TeamMember.telegram_user_id == 99999999)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].telegram_username == "brand_new"
    assert rows[0].real_name == "Newbie Smith"
    assert rows[0].active is True


def test_upsert_member_creates_inactive_team_row_for_bot_account(session):
    """FR-CR-05-27 — auto-created rows for obvious bot accounts
    start `active=False` so they don't pollute the LLM's owner
    candidates. Operator can flip on the sheet if needed."""
    from app.models import TeamMember

    upsert_member(
        session,
        chat_id=-100,
        user_id=8675309,
        username="ops1_notif",
        first_name="Ops Notif Bot",
    )
    session.flush()
    row = (
        session.query(TeamMember)
        .filter(TeamMember.telegram_user_id == 8675309)
        .first()
    )
    assert row is not None
    assert row.active is False


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
