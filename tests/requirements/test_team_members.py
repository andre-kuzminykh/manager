"""FR-CR-05-10 — Cross-channel team registry.

The registry is the AUTHORITATIVE source of «who can be assigned a
task», unified across Slack and Telegram. This module covers the
service-level reads, the auto-seed paths from existing
chat-members / Slack employees, and the bidirectional sheet
round-trip helpers.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.models import Employee, TeamMember, TelegramChatMember
from app.services.team_members import (
    SHEET_HEADERS,
    as_known_employees,
    find_by_slack_user_id,
    find_by_telegram_user_id,
    list_active,
    seed_from_chat_members,
    seed_from_slack_employees,
    to_sheet_rows,
    upsert_from_sheet_rows,
)


# --------------------------------------------------------------------------- #
# Read paths
# --------------------------------------------------------------------------- #


def test_list_active_excludes_inactive(session):
    session.add_all(
        [
            TeamMember(real_name="Alice", telegram_user_id=1, active=True),
            TeamMember(real_name="Bob", telegram_user_id=2, active=False),
        ]
    )
    session.flush()
    out = list_active(session)
    assert {m.real_name for m in out} == {"Alice"}


def test_as_known_employees_prefers_telegram_id(session):
    """For TG ingest the LLM should round-trip the numeric TG id —
    the post-classification handler uses it to DM the assignee."""
    session.add_all(
        [
            TeamMember(
                real_name="Andre Kuzminykh",
                telegram_user_id=222968032,
                telegram_username="andre_andreevich",
                slack_user_id="U09SLACK01",
                active=True,
            ),
        ]
    )
    session.flush()
    out = as_known_employees(session, prefer_telegram=True)
    assert len(out) == 1
    e = out[0]
    assert e["slack_user_id"] == "222968032"  # TG id, despite the field name
    assert e["display_name"] == "@andre_andreevich"
    assert e["real_name"] == "Andre Kuzminykh"


def test_as_known_employees_falls_back_when_preferred_id_missing(session):
    """A team member with only a Slack id should still appear in the
    TG-ingest list — the LLM round-trips the Slack uid; downstream
    DM delivery just won't reach them on TG (which is the expected
    behaviour for Slack-only teammates)."""
    session.add_all(
        [
            TeamMember(
                real_name="Slack Only",
                slack_user_id="USLACK999",
                active=True,
            ),
        ]
    )
    session.flush()
    out = as_known_employees(session, prefer_telegram=True)
    assert len(out) == 1
    assert out[0]["slack_user_id"] == "USLACK999"


def test_as_known_employees_skips_inactive(session):
    session.add_all(
        [
            TeamMember(real_name="A", telegram_user_id=1, active=True),
            TeamMember(real_name="B", telegram_user_id=2, active=False),
        ]
    )
    session.flush()
    out = as_known_employees(session)
    assert {e["real_name"] for e in out} == {"A"}


def test_find_by_telegram_user_id_returns_match(session):
    session.add(TeamMember(real_name="Petya", telegram_user_id=42))
    session.flush()
    found = find_by_telegram_user_id(session, 42)
    assert found is not None
    assert found.real_name == "Petya"
    assert find_by_telegram_user_id(session, 999) is None


def test_find_by_slack_user_id_returns_match(session):
    session.add(TeamMember(real_name="Vasya", slack_user_id="UVASYA"))
    session.flush()
    found = find_by_slack_user_id(session, "UVASYA")
    assert found is not None
    assert find_by_slack_user_id(session, "UMISSING") is None


# --------------------------------------------------------------------------- #
# Auto-seed
# --------------------------------------------------------------------------- #


def test_seed_from_chat_members_creates_one_row_per_distinct_user(session):
    """Same user speaking in multiple chats deduplicates to ONE
    team_members row keyed by telegram_user_id."""
    session.add_all(
        [
            TelegramChatMember(
                chat_id=-100, user_id=42, username="petya", first_name="Petya",
                last_seen_at=datetime.now(timezone.utc),
            ),
            TelegramChatMember(
                chat_id=-200, user_id=42, username="petya", first_name="Petya",
                last_seen_at=datetime.now(timezone.utc),
            ),
            TelegramChatMember(
                chat_id=-100, user_id=99, username="vasya",
                last_seen_at=datetime.now(timezone.utc),
            ),
        ]
    )
    session.flush()

    added = seed_from_chat_members(session)
    assert added == 2
    assert {m.telegram_user_id for m in list_active(session)} == {42, 99}


def test_seed_from_chat_members_is_idempotent(session):
    """Re-running seed must not create duplicates."""
    session.add(
        TelegramChatMember(
            chat_id=-100, user_id=42, username="petya",
            last_seen_at=datetime.now(timezone.utc),
        )
    )
    session.flush()

    first = seed_from_chat_members(session)
    second = seed_from_chat_members(session)
    assert first == 1
    assert second == 0


def test_seed_from_slack_employees_creates_rows_and_skips_bots(session):
    session.add_all(
        [
            Employee(
                slack_user_id="U1", real_name="Alice", email="a@x.com",
                title="PM", is_bot=False,
            ),
            Employee(slack_user_id="UBOT", display_name="bot", is_bot=True),
        ]
    )
    session.flush()
    added = seed_from_slack_employees(session)
    assert added == 1
    out = {m.slack_user_id for m in list_active(session)}
    assert out == {"U1"}


# --------------------------------------------------------------------------- #
# Sheet round-trip
# --------------------------------------------------------------------------- #


def test_to_sheet_rows_starts_with_header(session):
    session.add(
        TeamMember(
            real_name="Andre",
            telegram_user_id=42,
            telegram_username="andre",
            slack_user_id="U1",
            role="Founder",
            email="a@x.com",
            active=True,
        )
    )
    session.flush()
    rows = to_sheet_rows(session)
    assert rows[0] == SHEET_HEADERS
    assert len(rows) == 2
    body = rows[1]
    # id, real_name, tg_id, tg_username, slack_uid, role, email, active, notes
    assert body[1] == "Andre"
    assert body[2] == "42"
    assert body[3] == "andre"
    assert body[4] == "U1"
    assert body[5] == "Founder"
    assert body[6] == "a@x.com"
    assert body[7] == "true"


def test_upsert_from_sheet_rows_inserts_new_then_updates_by_id(session):
    """First pull inserts; second pull (with the assigned id) updates
    the same row in place."""
    rows = [
        ["", "Andre", "42", "andre", "U1", "Founder", "a@x.com", "true", ""],
    ]
    updated, inserted = upsert_from_sheet_rows(session, rows)
    assert (updated, inserted) == (0, 1)
    [member] = list_active(session)
    assert member.real_name == "Andre"

    # Operator edits the role on the sheet; pull again with the now-
    # assigned id.
    rows = [
        [str(member.id), "Andre K.", "42", "andre", "U1", "CEO", "a@x.com", "true", ""],
    ]
    updated, inserted = upsert_from_sheet_rows(session, rows)
    assert (updated, inserted) == (1, 0)
    refreshed = find_by_telegram_user_id(session, 42)
    assert refreshed is not None
    assert refreshed.real_name == "Andre K."
    assert refreshed.role == "CEO"


def test_upsert_from_sheet_rows_matches_by_telegram_id_when_no_id(session):
    """Operator added a row by hand without filling in the `id` column.
    The pull should still find the existing DB row by tg_user_id and
    update it rather than inserting a duplicate."""
    session.add(
        TeamMember(real_name="OldName", telegram_user_id=42, active=True)
    )
    session.flush()
    rows = [
        ["", "NewName", "42", "andre", "", "", "", "true", ""],
    ]
    updated, inserted = upsert_from_sheet_rows(session, rows)
    assert (updated, inserted) == (1, 0)
    refreshed = find_by_telegram_user_id(session, 42)
    assert refreshed.real_name == "NewName"


def test_upsert_from_sheet_rows_normalises_active_to_bool(session):
    """The sheet round-trips `true` / `false` strings, but operators
    type all sorts of variants (`да`, `1`, `yes`). All truthy
    spellings must collapse to active=True; explicitly «no» / «false»
    inactivates; empty cell defaults to active=True (operator added
    a row by hand without filling in the column)."""
    rows = [
        ["", "A1", "1", "", "", "", "", "true", ""],
        ["", "A2", "2", "", "", "", "", "yes", ""],
        ["", "A3", "3", "", "", "", "", "да", ""],
        ["", "A4", "4", "", "", "", "", "no", ""],
        ["", "A5", "5", "", "", "", "", "", ""],   # default -> active
    ]
    upsert_from_sheet_rows(session, rows)
    actives = {m.real_name for m in list_active(session)}
    assert actives == {"A1", "A2", "A3", "A5"}
