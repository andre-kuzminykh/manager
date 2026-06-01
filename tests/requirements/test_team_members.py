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
    """FR-CR-05-10 / FR-CR-05-25 — seeding from the Supabase view
    inserts one `team_members` row per distinct user_id. When the
    view ships a dedicated `sender_username` column (FR-CR-05-25),
    real_name and telegram_username are populated from separate
    fields. When it doesn't, fall back to the legacy heuristic."""
    from app.services.team_members import seed_from_telegram_source

    class _FakeReader:
        configured = True

        def distinct_users(self):
            return [
                # Both fields filled in (modern view shape).
                {"user_id": 42, "user_name": "Артем Соколов",
                 "username": "artem_sokolov"},
                # No username column at all — heuristic guess.
                {"user_id": 99, "user_name": "Andre Kuzminykh",
                 "username": None},
                # Empty everything — only id known.
                {"user_id": 700, "user_name": None, "username": None},
                # Heuristic-only legacy row: ascii token → username.
                {"user_id": 800, "user_name": "petya", "username": None},
            ]

    added = seed_from_telegram_source(session, _FakeReader())
    assert added == 4

    out = {m.telegram_user_id: m for m in list_active(session)}
    assert set(out.keys()) == {42, 99, 700, 800}
    # FR-CR-05-25 — both fields populate cleanly when the view
    # has them separately.
    assert out[42].real_name == "Артем Соколов"
    assert out[42].telegram_username == "artem_sokolov"
    # «Andre Kuzminykh» has a space → still treated as real_name
    # (no username available).
    assert out[99].real_name == "Andre Kuzminykh"
    assert out[99].telegram_username is None
    # No data anywhere — row exists for the id alone.
    assert out[700].telegram_username is None
    assert out[700].real_name is None
    # Legacy heuristic still works for views without sender_username.
    assert out[800].telegram_username == "petya"
    assert out[800].real_name is None


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


def test_upsert_from_sheet_rows_skips_blank_rows(session):
    """BUG-FIX 2026-06-01 — a completely blank trailing sheet row (no id /
    tg / slack / name) must be SKIPPED, not inserted. Otherwise each periodic
    pull accumulates a new blank TeamMember (12.9k seen in prod over 11 days)."""
    rows = [
        ["", "Andre", "42", "andre", "U1", "Founder", "a@x.com", "true", ""],
        ["", "", "", "", "", "", "", "", ""],          # blank trailing row
        ["", "", "", "", "", "", "", "true", ""],       # blank but active=true
        [""] * 3,                                        # short blank row
    ]
    updated, inserted = upsert_from_sheet_rows(session, rows)
    assert (updated, inserted) == (0, 1)               # only the real row
    members = list_active(session)
    assert len(members) == 1 and members[0].real_name == "Andre"

    # A second pull with the same blank rows still inserts nothing new.
    updated, inserted = upsert_from_sheet_rows(session, rows[1:])
    assert inserted == 0


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


def test_team_sheet_push_appends_only_new_rows(session):
    """FR-CR-05-27 — `push` is non-destructive: appends only DB
    rows that aren't on the sheet yet (matched by id /
    telegram_user_id / slack_user_id). Operator-edited cells on
    existing sheet rows are NEVER touched."""
    from app.sync.team_sheet import TeamSheetSync

    # Two rows in DB.
    session.add_all(
        [
            TeamMember(
                real_name="Existing Row",
                telegram_user_id=42,
                telegram_username="petya",
                active=True,
            ),
            TeamMember(
                real_name="Brand New",
                telegram_user_id=99,
                active=True,
            ),
        ]
    )
    session.flush()

    # Sheet already has the existing row (by tg_user_id) but with
    # the operator's custom edits. The new row (uid 99) is missing.
    existing_sheet = [
        ["id", "real_name", "telegram_user_id", "telegram_username",
         "slack_user_id", "role", "email", "active", "notes"],
        ["1", "Operator's Custom Name", "42", "custom_handle",
         "", "Founder", "petya@x.com", "true", "do not touch"],
    ]
    appended_rows: list[list[str]] = []
    cleared = {"called": False}
    written: list[list[list[str]]] = []

    class _StubSync(TeamSheetSync):
        def __init__(self):  # bypass googleapiclient build
            self._service = None
            self._spreadsheet_id = "stub"
            self._sheet_name = "Team"

        def _read_all(self):  # type: ignore[override]
            return existing_sheet

        def _append(self, rows):  # type: ignore[override]
            appended_rows.extend(rows)

        def _clear(self):  # type: ignore[override]
            cleared["called"] = True

        def _write(self, values):  # type: ignore[override]
            written.append(values)

    sync = _StubSync()
    pushed = sync.push(session)

    # Operator's row was NOT cleared / overwritten.
    assert cleared["called"] is False
    assert written == []
    # Only the brand-new row got appended.
    assert pushed == 1
    assert len(appended_rows) == 1
    appended = appended_rows[0]
    # Body row layout: id / real_name / tg_id / tg_username / slack_id / …
    assert appended[1] == "Brand New"
    assert appended[2] == "99"


def test_team_sheet_push_writes_full_table_when_sheet_empty(session):
    """First-time bootstrap: sheet has no rows yet → push writes
    the full DB state (header + body) so the operator has a
    starting point."""
    from app.sync.team_sheet import TeamSheetSync

    session.add(
        TeamMember(
            real_name="First Member",
            telegram_user_id=42,
            telegram_username="petya",
            active=True,
        )
    )
    session.flush()

    written_payloads: list[list[list[str]]] = []
    appended_payloads: list[list[list[str]]] = []

    class _Empty(TeamSheetSync):
        def __init__(self):
            self._service = None
            self._spreadsheet_id = "stub"
            self._sheet_name = "Team"

        def _read_all(self):  # type: ignore[override]
            return []

        def _write(self, values):  # type: ignore[override]
            written_payloads.append(values)

        def _append(self, rows):  # type: ignore[override]
            appended_payloads.extend(rows)

    pushed = _Empty().push(session)
    assert pushed == 1
    assert written_payloads, "first-time push should call _write with the full table"
    body = written_payloads[0][1:]
    assert body[0][1] == "First Member"


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


def test_upsert_from_sheet_rows_merges_duplicates_by_unique_column(session):
    """FR-CR-05-29 — the auto-seed often produces TWO rows for the
    same teammate: one from `chat_members` (numeric TG id only)
    and one from Slack `employees` (Slack uid only). The operator
    consolidates them on the Sheet by editing one row to carry
    BOTH ids. Without the merge logic, the pull crashes on
    `UniqueViolation` because the OTHER row still owns that id.

    Expected behaviour: when applying the update would conflict
    on a UNIQUE column with a DIFFERENT row, that other row is
    deleted (the operator is clearly merging) and the update
    lands cleanly."""
    # The two auto-seeded rows the operator wants to merge.
    tg_only = TeamMember(
        real_name="Andre",
        telegram_user_id=222968032,
        telegram_username=None,
        slack_user_id=None,
        active=True,
    )
    slack_only = TeamMember(
        real_name="Andre",
        telegram_user_id=None,
        slack_user_id="U09LH2FGALC",
        active=True,
    )
    session.add_all([tg_only, slack_only])
    session.flush()
    canonical_id = tg_only.id  # operator keeps this row

    # Operator's edited Sheet row carries BOTH ids on the
    # canonical row.
    rows = [
        [
            str(canonical_id),
            "Андрей Кузьминых",
            "222968032",
            "",                     # username still blank
            "U09LH2FGALC",
            "AI Lead",
            "",
            "true",
            "",
        ],
    ]
    updated, inserted = upsert_from_sheet_rows(session, rows)
    assert (updated, inserted) == (1, 0)

    # Orphan row (slack_only) is gone.
    rows_left = list_active(session)
    assert len(rows_left) == 1
    survivor = rows_left[0]
    assert survivor.id == canonical_id
    assert survivor.real_name == "Андрей Кузьминых"
    assert survivor.telegram_user_id == 222968032
    assert survivor.slack_user_id == "U09LH2FGALC"
    assert survivor.role == "AI Lead"


# --------------------------------------------------------------------------- #
# FR-CR-05-60 — soft-deactivate rows removed from the sheet
# --------------------------------------------------------------------------- #


def test_upsert_from_sheet_rows_soft_deactivates_missing(session):
    """Operator removes a row from the Sheet — the next pull
    must set `active=False` on the corresponding DB row so the
    LLM owner picker stops suggesting them.

    Reproduces the production bug: «Aisala Kambekova» was
    deleted from the Team Sheet but stayed `active=True` in DB
    and kept landing as the assignee on extracted tasks."""
    # Seed two team members.
    session.add_all([
        TeamMember(
            real_name="Aisala", telegram_user_id=111,
            telegram_username="aisala", active=True,
            role="something",
        ),
        TeamMember(
            real_name="Юля", telegram_user_id=222,
            telegram_username="yulia", active=True,
            role="Time management coordinator",
            notes="согласует встречи, планирует календарь",
        ),
    ])
    session.flush()
    assert len(list_active(session)) == 2

    # Operator removes Aisala from the sheet — only Юля remains.
    # Pull payload reflects the new sheet state.
    yulia = find_by_telegram_user_id(session, 222)
    assert yulia is not None
    rows = [
        [
            str(yulia.id), "Юля", "222", "yulia", "",
            "Time management coordinator", "", "true",
            "согласует встречи, планирует календарь",
        ],
    ]
    updated, inserted = upsert_from_sheet_rows(session, rows)
    # Юля refreshed (no real change) + Aisala soft-deactivated.
    assert inserted == 0
    assert updated >= 1  # at least the deactivation

    # Aisala is no longer surfaced via list_active.
    actives = list_active(session)
    assert len(actives) == 1
    assert actives[0].real_name == "Юля"

    # Aisala still exists in DB but flagged inactive.
    aisala = find_by_telegram_user_id(session, 111)
    assert aisala is not None  # not hard-deleted
    assert aisala.active is False


def test_upsert_from_sheet_rows_does_not_deactivate_when_pull_empty(session):
    """Defence-in-depth: if the pull returns NO rows (sheet was
    cleared by accident or returned an empty range), DON'T
    deactivate everyone. The seen-set guard requires at least
    one row to anchor the «what counts as missing» logic."""
    session.add(
        TeamMember(
            real_name="Andre", telegram_user_id=42,
            telegram_username="andre", active=True,
        )
    )
    session.flush()

    updated, inserted = upsert_from_sheet_rows(session, [])
    assert (updated, inserted) == (0, 0)
    actives = list_active(session)
    assert len(actives) == 1
    assert actives[0].real_name == "Andre"


# --------------------------------------------------------------------------- #
# FR-CR-05-142a / 142b — `pick_meeting_owner_fallback` cascade.
# --------------------------------------------------------------------------- #


def test_pick_meeting_owner_fallback_picks_principal_among_participants():
    """Cascade pass 1: principal (notes/role mention CEO/founder/
    principal) wins."""
    from app.services.team_members import pick_meeting_owner_fallback

    employees = [
        {"slack_user_id": "U1", "real_name": "Артем",
         "role": "CEO", "notes": "founder, principal"},
        {"slack_user_id": "U2", "real_name": "Алина",
         "role": "IR", "notes": "investor relations"},
        {"slack_user_id": "U3", "real_name": "Andre",
         "role": "AI Lead", "notes": "admin"},
    ]
    out = pick_meeting_owner_fallback(
        known_employees=employees,
        participants_real_names=["Артем", "Алина"],
    )
    assert out == "U1"


def test_pick_meeting_owner_fallback_skips_admin_uid_via_env(monkeypatch):
    """FR-CR-05-134 compatibility — never pick the admin row
    even when admin's notes / role would otherwise win."""
    from app.config import get_settings
    from app.services.team_members import pick_meeting_owner_fallback

    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "U_ADMIN")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        employees = [
            # Admin marked as principal — must STILL be skipped.
            {"slack_user_id": "U_ADMIN", "real_name": "Andre Admin",
             "role": "Founder / CEO", "notes": "principal admin"},
            {"slack_user_id": "U2", "real_name": "Алина",
             "role": "IR", "notes": ""},
        ]
        out = pick_meeting_owner_fallback(
            known_employees=employees,
            participants_real_names=["Andre Admin", "Алина"],
        )
        assert out == "U2"  # Алина, NOT Andre Admin.
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_pick_meeting_owner_fallback_excludes_topic_forbidden_teammates():
    """FR-CR-05-142b — when a teammate's notes say «не вести
    fundraising-задачи» AND the task topic is fundraising,
    skip them in favour of an unrestricted teammate."""
    from app.services.team_members import pick_meeting_owner_fallback

    employees = [
        # Дроздов would otherwise win as a present principal.
        {"slack_user_id": "U_DROZDOV", "real_name": "Дима Дроздов",
         "role": "Аналитик",
         "notes": "Research; не вести fundraising-задачи"},
        # Седов is fundraising — should win for fundraising tasks.
        {"slack_user_id": "U_SEDOV", "real_name": "Дмитрий Седов",
         "role": "Финансовый Советник",
         "notes": "Ведёт fundraising / IR"},
    ]
    out = pick_meeting_owner_fallback(
        known_employees=employees,
        participants_real_names=["Дима Дроздов", "Дмитрий Седов"],
        topic_keywords=["fundraising", "ir"],
    )
    assert out == "U_SEDOV"


def test_pick_meeting_owner_fallback_honours_ne_uchastvuet_phrasing():
    """FR-CR-05-142b — operator's actual notes use «не
    участвует в Fundrising sync» (not «не вести X-задачи»).
    The fallback must still recognise this phrasing AND the
    operator's typo «Fundrising» (vs «Fundraising»). Pinned
    against the real production notes for Дима Дроздов."""
    from app.services.team_members import (
        infer_topic_keywords_from_text,
        pick_meeting_owner_fallback,
    )

    drozdov_notes = (
        "ВСЕ, ЧТО СВЯЗАНО С ФОНДАМИ\n"
        "Коннекты со встреч\n"
        "Поиск выходов на фонды\n"
        "Аутрич (почта, линк) // не участвует в Fundrising sync"
    )
    sedov_notes = (
        "Ведёт все fundraising / IR задачи: общение с инвесторами, "
        "варанты, экземпляры контрактов, fund close"
    )
    employees = [
        {"slack_user_id": "U_DROZDOV", "real_name": "Дима Дроздов",
         "role": "Head of Network", "notes": drozdov_notes},
        {"slack_user_id": "U_SEDOV", "real_name": "Дмитрий Седов",
         "role": "Финансовый Советник Артема", "notes": sedov_notes},
    ]

    # Topic inferred from operator's actual meeting title (with
    # the «Fundrising» typo).
    keywords = infer_topic_keywords_from_text("01/05 - Fundrising sync")
    assert "fundrais" in keywords or "fundrising" in keywords

    out = pick_meeting_owner_fallback(
        known_employees=employees,
        participants_real_names=["Дима Дроздов", "Дмитрий Седов"],
        topic_keywords=keywords,
    )
    # Дроздов EXCLUDED via «не участвует в Fundrising sync»
    # override; cascade lands on Седов.
    assert out == "U_SEDOV"


def test_pick_meeting_owner_fallback_returns_none_with_no_participants():
    from app.services.team_members import pick_meeting_owner_fallback

    employees = [
        {"slack_user_id": "U1", "real_name": "Артем",
         "role": "CEO", "notes": "principal"},
    ]
    assert pick_meeting_owner_fallback(
        known_employees=employees, participants_real_names=[],
    ) is None


def test_pick_meeting_owner_fallback_last_resort_first_participant():
    """Pass 3 — when nobody is principal AND every present
    teammate is forbidden, fall back to first participant.
    Better than null per operator's «всегда ответственный
    должен быть»."""
    from app.services.team_members import pick_meeting_owner_fallback

    employees = [
        {"slack_user_id": "U_A", "real_name": "А.",
         "role": "Analyst", "notes": "не вести fundraising-задачи"},
        {"slack_user_id": "U_B", "real_name": "Б.",
         "role": "Analyst", "notes": "не вести fundraising"},
    ]
    out = pick_meeting_owner_fallback(
        known_employees=employees,
        participants_real_names=["А.", "Б."],
        topic_keywords=["fundraising"],
    )
    assert out == "U_A"  # last-resort first present.


def test_infer_topic_keywords_from_text_recognises_fundraising_signals():
    from app.services.team_members import infer_topic_keywords_from_text

    assert "fundraising" in infer_topic_keywords_from_text(
        "01/05 - Fundraising sync"
    )
    assert "fundraising" in infer_topic_keywords_from_text(
        "Раунд Humanoid — first close"
    )
    # No fundraising signal → empty.
    assert infer_topic_keywords_from_text("Standup") == []
    assert infer_topic_keywords_from_text("") == []


# --------------------------------------------------------------------------- #
# FR-CR-05-145 — Python-side defense for «не участвует в X» / «не вести X»
# --------------------------------------------------------------------------- #


def test_filter_participants_drops_drozdov_on_fundraising_topic():
    """FR-CR-05-145 — operator regression «опять дима дроздов
    во фандрайзинге участвует - он не должен». Even when the
    LLM puts Дроздов into the participants list, the Python
    post-filter must drop him because his notes contain
    «не участвует в Fundrising sync» and the topic_keywords
    include «fundraising»."""
    from app.services.team_members import (
        filter_participants_by_notes_forbid,
        infer_topic_keywords_from_text,
    )

    drozdov_notes = (
        "ВСЕ, ЧТО СВЯЗАНО С ФОНДАМИ\n"
        "Коннекты со встреч\n"
        "Поиск выходов на фонды\n"
        "Аутрич (почта, линк) // не участвует в Fundrising sync"
    )
    employees = [
        {"real_name": "Дима Дроздов", "notes": drozdov_notes},
        {"real_name": "Дмитрий Седов",
         "notes": "Ведёт fundraising / IR"},
        {"real_name": "Артем Соколов",
         "notes": "founder, principal"},
    ]
    # Even when Zoom auto-title is "Artem Sokolov's Zoom Meeting"
    # (no fundraising keyword), the transcript content surfaces
    # the topic.
    topic_text = (
        "Artem Sokolov's Zoom Meeting\n"
        "Прошлись по fundraising pipeline, Schaeffler investor "
        "update, Sanders Capital, варанты, term sheet."
    )
    keywords = infer_topic_keywords_from_text(topic_text)
    assert "fundraising" in keywords  # sanity check

    kept, dropped = filter_participants_by_notes_forbid(
        ["Дима Дроздов", "Дмитрий Седов", "Артем Соколов"],
        known_employees=employees,
        topic_keywords=keywords,
    )
    assert kept == ["Дмитрий Седов", "Артем Соколов"]
    assert dropped == ["Дима Дроздов"]


def test_filter_participants_no_op_when_no_topic_keywords():
    """Empty topic_keywords (e.g. internal sync without
    fundraising signal) → no filtering."""
    from app.services.team_members import (
        filter_participants_by_notes_forbid,
    )

    employees = [
        {"real_name": "Дима Дроздов",
         "notes": "не участвует в Fundrising sync"},
    ]
    kept, dropped = filter_participants_by_notes_forbid(
        ["Дима Дроздов"],
        known_employees=employees,
        topic_keywords=[],
    )
    assert kept == ["Дима Дроздов"]
    assert dropped == []


def test_filter_participants_unknown_name_passes_through():
    """A real_name not in known_employees has no notes to check,
    so we don't drop it (defensive — let the regular pipeline
    handle unknown names)."""
    from app.services.team_members import (
        filter_participants_by_notes_forbid,
    )

    employees = [{"real_name": "Дима Дроздов",
                  "notes": "не вести fundraising"}]
    kept, dropped = filter_participants_by_notes_forbid(
        ["Stranger", "Дима Дроздов"],
        known_employees=employees,
        topic_keywords=["fundraising"],
    )
    assert kept == ["Stranger"]
    assert dropped == ["Дима Дроздов"]
