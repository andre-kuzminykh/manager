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


def test_as_known_employees_carries_role_and_notes(session):
    """FR-CR-05-12 — role + notes propagate so the owner LLM can
    disambiguate same-first-name teammates."""
    session.add_all(
        [
            TeamMember(
                real_name="Alina Founder",
                telegram_user_id=1,
                role="founder",
                notes="deals with international expansion",
                active=True,
            ),
            TeamMember(
                real_name="Alina Manager",
                telegram_user_id=2,
                role="project manager / аналитик",
                notes="",
                active=True,
            ),
        ]
    )
    session.flush()
    out = sorted(as_known_employees(session), key=lambda e: e["slack_user_id"])
    assert len(out) == 2
    assert out[0]["role"] == "founder"
    assert out[0]["notes"] == "deals with international expansion"
    assert out[1]["role"] == "project manager / аналитик"
    assert out[1]["notes"] == ""


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


def test_looks_like_bot_heuristics():
    """FR-CR-05-13 — the bot-detection heuristic must catch the
    common bot-name patterns from real chat_members traffic
    (`CEO_office1 bot`, `notif_bot`, etc.) without false-positiving
    real human names with «bot» as a substring buried inside (e.g.
    «Bobotov»)."""
    from app.services.team_members import _looks_like_bot

    # True positives.
    assert _looks_like_bot("CEO_office1 bot", None)
    assert _looks_like_bot(None, "notif_bot")
    assert _looks_like_bot("support_assistant", None)
    assert _looks_like_bot("CRM Webhook", "crm_webhook")
    # False positives we don't want — real names with substrings.
    # Note: the current heuristic is intentionally conservative, so
    # «Бот» as a Russian surname WILL flag (operator can flip on
    # the sheet). We pin the «space + bot» / «_bot» behaviour:
    assert not _looks_like_bot("Bobotov", None)
    assert not _looks_like_bot("Petya Pupkin", None)
    assert not _looks_like_bot("Алина", "alina")


def test_seed_from_chat_members_marks_bot_accounts_inactive(session):
    """FR-CR-05-13 — bot accounts seeded from chat_members default
    `active=False` so they never make it into the LLM owner-
    candidate list. Operator can flip on the sheet if a row was
    misclassified."""
    session.add_all(
        [
            TelegramChatMember(
                chat_id=-100, user_id=42, username="petya", first_name="Petya",
                last_seen_at=datetime.now(timezone.utc),
            ),
            TelegramChatMember(
                chat_id=-100, user_id=99, first_name="CEO_office1 bot",
                last_seen_at=datetime.now(timezone.utc),
            ),
        ]
    )
    session.flush()
    seed_from_chat_members(session)
    actives = {m.real_name or m.telegram_username for m in list_active(session)}
    assert "petya" in actives or "Petya" in actives
    assert "CEO_office1 bot" not in actives


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


def test_backfill_fills_blank_team_members_from_chat_members(session):
    """FR-CR-05-23 — one-shot backfill catches rows seeded before
    FR-CR-05-21 auto-enrich landed: walks every team_members row,
    pulls username / first+last from the most recent
    telegram_chat_members observation for that user_id, and fills
    in BLANK fields. Operator-edited values are preserved."""
    from app.services.team_members import (
        backfill_team_members_from_chat_members,
    )
    from datetime import datetime, timezone as _tz

    # Sparse row — only id, no name / username yet.
    session.add(
        TeamMember(
            telegram_user_id=97239970,
            telegram_username=None,
            real_name=None,
            active=True,
        )
    )
    # Operator-curated row — must be preserved.
    session.add(
        TeamMember(
            telegram_user_id=222968032,
            telegram_username="custom_handle",
            real_name="Andre Custom",
            active=True,
        )
    )
    # Listener has observed both users speaking in some chat.
    session.add(
        TelegramChatMember(
            chat_id=-1001234, user_id=97239970,
            username="artem_sokolov",
            first_name="Артем", last_name="Соколов",
            last_seen_at=datetime.now(_tz.utc),
        )
    )
    session.add(
        TelegramChatMember(
            chat_id=-1001234, user_id=222968032,
            username="andre_andreevich",
            first_name="Андрей", last_name="Кузьминых",
            last_seen_at=datetime.now(_tz.utc),
        )
    )
    session.flush()

    changed = backfill_team_members_from_chat_members(session)
    assert changed == 1  # Artem's sparse row got filled

    artem = (
        session.query(TeamMember)
        .filter(TeamMember.telegram_user_id == 97239970)
        .first()
    )
    assert artem.telegram_username == "artem_sokolov"
    assert artem.real_name == "Артем Соколов"

    # Operator's edits preserved.
    andre = (
        session.query(TeamMember)
        .filter(TeamMember.telegram_user_id == 222968032)
        .first()
    )
    assert andre.telegram_username == "custom_handle"
    assert andre.real_name == "Andre Custom"


def test_enrich_from_bot_api_populates_blank_fields(session):
    """FR-CR-05-24 — Bot API getChat returns user profile for
    users the bot has interacted with. Sparse `team_members` rows
    get their `telegram_username` / `real_name` filled in from
    the response. Operator edits preserved."""
    from app.services.team_members import enrich_team_members_from_bot_api

    session.add(TeamMember(telegram_user_id=97239970, active=True))
    session.add(
        TeamMember(
            telegram_user_id=222968032,
            telegram_username="custom",
            real_name="Custom Name",
            active=True,
        )
    )
    session.flush()

    class _FakeSender:
        enabled = True

        def __init__(self):
            self.calls = []

        def get_chat(self, *, chat_id):
            self.calls.append(chat_id)
            if chat_id == 97239970:
                return {
                    "id": 97239970,
                    "type": "private",
                    "username": "artem_sokolov",
                    "first_name": "Артем",
                    "last_name": "Соколов",
                }
            # Operator-curated row should be skipped (its fields are
            # already populated, so we never call getChat).
            raise AssertionError(f"unexpected getChat call for {chat_id}")

    sender = _FakeSender()
    changed = enrich_team_members_from_bot_api(session, sender)
    assert changed == 1
    assert sender.calls == [97239970]

    artem = (
        session.query(TeamMember)
        .filter(TeamMember.telegram_user_id == 97239970)
        .first()
    )
    assert artem.telegram_username == "artem_sokolov"
    assert artem.real_name == "Артем Соколов"


def test_enrich_from_bot_api_silently_skips_unknown_users(session):
    """`getChat` returns `{}` (or no `id`) for users the bot has
    never seen. Those rows stay sparse — no crash, no garbage."""
    from app.services.team_members import enrich_team_members_from_bot_api

    session.add(TeamMember(telegram_user_id=99999999, active=True))
    session.flush()

    class _StubSender:
        enabled = True

        def get_chat(self, *, chat_id):
            return {}

    assert enrich_team_members_from_bot_api(session, _StubSender()) == 0


def test_enrich_from_bot_api_noop_when_sender_disabled(session):
    """No bot token ⇒ no Bot API calls ⇒ early-return."""
    from app.services.team_members import enrich_team_members_from_bot_api

    class _Disabled:
        enabled = False

        def get_chat(self, **kw):  # pragma: no cover
            raise AssertionError("must not be called")

    assert enrich_team_members_from_bot_api(session, _Disabled()) == 0
    assert enrich_team_members_from_bot_api(session, None) == 0


def test_backfill_no_op_when_chat_members_empty(session):
    """No `chat_members` data ⇒ nothing to fill ⇒ no rows changed."""
    from app.services.team_members import (
        backfill_team_members_from_chat_members,
    )

    session.add(
        TeamMember(telegram_user_id=42, active=True)
    )
    session.flush()
    assert backfill_team_members_from_chat_members(session) == 0


def test_seed_from_telegram_source_pulls_distinct_users(session):
    """FR-CR-05-10 — seeding from the Supabase view inserts one
    `team_members` row per distinct user_id we've ever seen send a
    message, even when the live listener hasn't observed them yet.

    Reader is faked here — the unit test pins the parsing /
    dedup logic that lives in `seed_from_telegram_source`."""
    from app.services.team_members import seed_from_telegram_source

    class _FakeReader:
        configured = True

        def distinct_users(self):
            return [
                {"user_id": 42, "user_name": "petya"},
                {"user_id": 99, "user_name": "Andre Kuzminykh"},
                {"user_id": 700, "user_name": None},
            ]

    added = seed_from_telegram_source(session, _FakeReader())
    assert added == 3

    out = {m.telegram_user_id: m for m in list_active(session)}
    assert set(out.keys()) == {42, 99, 700}
    # «petya» — single ASCII token → username, real_name=None.
    assert out[42].telegram_username == "petya"
    assert out[42].real_name is None
    # «Andre Kuzminykh» has a space → real_name, username=None.
    assert out[99].real_name == "Andre Kuzminykh"
    assert out[99].telegram_username is None
    # No name at all — both null but row still inserted (gives the
    # operator a starting line with the numeric id).
    assert out[700].telegram_username is None
    assert out[700].real_name is None


def test_seed_from_telegram_source_is_idempotent(session):
    """Re-running seed against a reader that returns the same users
    must NOT create duplicates."""
    from app.services.team_members import seed_from_telegram_source

    class _FakeReader:
        configured = True

        def distinct_users(self):
            return [{"user_id": 42, "user_name": "petya"}]

    first = seed_from_telegram_source(session, _FakeReader())
    second = seed_from_telegram_source(session, _FakeReader())
    assert (first, second) == (1, 0)


def test_seed_from_telegram_source_no_op_when_reader_unconfigured(session):
    from app.services.team_members import seed_from_telegram_source

    class _NoReader:
        configured = False

        def distinct_users(self):  # pragma: no cover
            raise AssertionError("must not be called")

    assert seed_from_telegram_source(session, _NoReader()) == 0
    assert seed_from_telegram_source(session, None) == 0


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
