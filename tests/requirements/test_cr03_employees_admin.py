"""Requirement coverage: FR-CR-03-1 (Employees directory),
FR-CR-03-2 (admin registry), NFR-CR-03-1 (Slack rate limits).

CR-03 Phase A: Employees directory + admin registry."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.models import Employee
from app.services import EmployeeDirectory, admin_slack_user_ids, is_admin, sync_admin_flags


# --------------------------------------------------------------------------- #
# admin_slack_user_ids / is_admin
# --------------------------------------------------------------------------- #


def test_admin_set_empty_by_default():
    assert admin_slack_user_ids(Settings()) == set()


def test_admin_set_parses_comma_separated():
    s = Settings(ADMIN_SLACK_USER_IDS="U1, U2,U3")
    assert admin_slack_user_ids(s) == {"U1", "U2", "U3"}


def test_admin_set_strips_empty_entries():
    s = Settings(ADMIN_SLACK_USER_IDS=" ,U1,, ,U2 ")
    assert admin_slack_user_ids(s) == {"U1", "U2"}


def test_is_admin_true_for_configured_user():
    s = Settings(ADMIN_SLACK_USER_IDS="U1,U2")
    assert is_admin("U1", s) is True
    assert is_admin("U3", s) is False


def test_is_admin_handles_missing_user():
    assert is_admin(None, Settings(ADMIN_SLACK_USER_IDS="U1")) is False
    assert is_admin("", Settings(ADMIN_SLACK_USER_IDS="U1")) is False


# --------------------------------------------------------------------------- #
# EmployeeDirectory.observed — first-sight, no Slack client
# --------------------------------------------------------------------------- #


def test_observed_creates_row_on_first_sight(session):
    d = EmployeeDirectory(client=None, settings=Settings())
    e = d.observed(session, slack_user_id="U1")
    assert e.slack_user_id == "U1"
    assert e.last_seen_at is not None
    assert session.query(Employee).count() == 1


def test_observed_reuses_row_on_subsequent_sightings(session):
    d = EmployeeDirectory(client=None, settings=Settings())
    first = d.observed(session, slack_user_id="U1")
    second = d.observed(session, slack_user_id="U1")
    assert first is second
    assert session.query(Employee).count() == 1


def test_observed_bumps_last_seen_at(session):
    d = EmployeeDirectory(client=None, settings=Settings())
    d.observed(
        session,
        slack_user_id="U1",
        seen_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    later = datetime(2026, 2, 1, tzinfo=timezone.utc)
    d.observed(session, slack_user_id="U1", seen_at=later)
    row = session.query(Employee).one()
    # SQLite strips tz info; compare on year/month to avoid flakiness.
    assert row.last_seen_at.year == 2026
    assert row.last_seen_at.month == 2


def test_observed_marks_admin_when_in_config(session):
    s = Settings(ADMIN_SLACK_USER_IDS="U-admin")
    d = EmployeeDirectory(client=None, settings=s)
    e = d.observed(session, slack_user_id="U-admin")
    assert e.is_admin is True
    other = d.observed(session, slack_user_id="U-dev")
    assert other.is_admin is False


def test_observed_flips_admin_when_config_changes(session):
    """Config can add/remove admins at runtime — observed() brings the flag
    up to date without needing a profile refresh."""
    s1 = Settings(ADMIN_SLACK_USER_IDS="")
    d1 = EmployeeDirectory(client=None, settings=s1)
    d1.observed(session, slack_user_id="U1")
    assert session.query(Employee).one().is_admin is False

    s2 = Settings(ADMIN_SLACK_USER_IDS="U1")
    d2 = EmployeeDirectory(client=None, settings=s2)
    d2.observed(session, slack_user_id="U1")
    assert session.query(Employee).one().is_admin is True


# --------------------------------------------------------------------------- #
# EmployeeDirectory._refresh_profile via Slack users.info stub
# --------------------------------------------------------------------------- #


class _SlackStub:
    def __init__(self, users: dict[str, dict]):
        self._users = users
        self.calls: list[str] = []

    def users_info(self, user: str):  # noqa: N802
        self.calls.append(user)
        data = self._users.get(user)
        if data is None:
            return {"ok": False}
        return {"ok": True, "user": data}


def _user_payload(uid, **overrides):
    base = {
        "id": uid,
        "team_id": "T1",
        "name": "handle",
        "is_bot": False,
        "tz": "Europe/Moscow",
        "profile": {
            "display_name_normalized": "Ivan",
            "real_name_normalized": "Иван Иванов",
            "email": "ivan@example.com",
            "title": "CEO",
        },
    }
    base.update(overrides)
    return base


def test_observed_pulls_profile_on_first_sight(session):
    stub = _SlackStub({"U1": _user_payload("U1")})
    d = EmployeeDirectory(client=stub, settings=Settings())
    e = d.observed(session, slack_user_id="U1")
    assert stub.calls == ["U1"]
    assert e.display_name == "Ivan"
    assert e.email == "ivan@example.com"
    assert e.title == "CEO"
    assert e.timezone == "Europe/Moscow"
    assert e.profile_refreshed_at is not None


def test_observed_does_not_re_fetch_within_ttl(session):
    stub = _SlackStub({"U1": _user_payload("U1")})
    d = EmployeeDirectory(
        client=stub, settings=Settings(EMPLOYEE_REFRESH_TTL_SECONDS=3600)
    )
    d.observed(session, slack_user_id="U1")
    d.observed(session, slack_user_id="U1")
    assert stub.calls == ["U1"]  # only one call


def test_observed_refreshes_after_ttl_expires(session):
    stub = _SlackStub({"U1": _user_payload("U1")})
    d = EmployeeDirectory(
        client=stub, settings=Settings(EMPLOYEE_REFRESH_TTL_SECONDS=1)
    )
    e = d.observed(session, slack_user_id="U1")
    # Rewind the profile refresh timestamp to simulate a stale cache.
    e.profile_refreshed_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    session.flush()
    d.observed(session, slack_user_id="U1")
    assert stub.calls == ["U1", "U1"]


def test_observed_survives_users_info_failure(session):
    class _Failing:
        def users_info(self, user):
            from slack_sdk.errors import SlackApiError

            raise SlackApiError(
                "rate", response=type("R", (), {"status_code": 429, "headers": {}, "data": {}})()
            )

    d = EmployeeDirectory(client=_Failing(), settings=Settings())
    e = d.observed(session, slack_user_id="U1")
    assert e.slack_user_id == "U1"
    # Row still exists even if refresh failed.
    assert e.email is None


def test_observed_marks_bot_as_is_bot(session):
    stub = _SlackStub({"U-bot": _user_payload("U-bot", is_bot=True)})
    d = EmployeeDirectory(client=stub, settings=Settings())
    e = d.observed(session, slack_user_id="U-bot")
    assert e.is_bot is True


# --------------------------------------------------------------------------- #
# sync_admin_flags backfill
# --------------------------------------------------------------------------- #


def test_sync_admin_flags_flips_bits(session):
    session.add(Employee(slack_user_id="U1", is_admin=False))
    session.add(Employee(slack_user_id="U2", is_admin=True))
    session.add(Employee(slack_user_id="U3", is_admin=False))
    session.flush()
    updated = sync_admin_flags(session, Settings(ADMIN_SLACK_USER_IDS="U1"))
    assert updated == 2  # U1 flipped to True, U2 flipped to False
    flags = {e.slack_user_id: e.is_admin for e in session.query(Employee).all()}
    assert flags == {"U1": True, "U2": False, "U3": False}


def test_sync_admin_flags_idempotent(session):
    session.add(Employee(slack_user_id="U1", is_admin=True))
    session.flush()
    updated = sync_admin_flags(session, Settings(ADMIN_SLACK_USER_IDS="U1"))
    assert updated == 0


# --------------------------------------------------------------------------- #
# handle_message / classify_and_persist hook
# --------------------------------------------------------------------------- #


def test_classify_and_persist_observes_the_author(
    patched_session_scope,
    services,
    sender,
    ack,
    bolt_context,
    slack_client,
    SessionFactory,
):
    """When handle_message runs, the author should get an Employee row."""
    from app.services import EmployeeDirectory
    from app.slack_bot.handlers.events import handle_message

    services.employees = EmployeeDirectory(client=None, settings=Settings())

    handle_message(
        event={
            "ts": "1.0",
            "user": "U-author",
            "text": "надо сделать задачу",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Emp-1"},
        client=slack_client,
        context=bolt_context,
        services=services,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        row = s.get(Employee, "U-author")
        assert row is not None
        assert row.last_seen_at is not None


def test_employees_directory_absent_does_not_break(
    patched_session_scope,
    services,
    sender,
    ack,
    bolt_context,
    slack_client,
    SessionFactory,
):
    """Back-compat: services constructed without employees= should still
    process messages end-to-end."""
    from app.slack_bot.handlers.events import handle_message

    services.employees = None  # as it was before CR-03

    handle_message(
        event={
            "ts": "2.0",
            "user": "U-author",
            "text": "надо задачу",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Emp-2"},
        client=slack_client,
        context=bolt_context,
        services=services,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        # No Employee row is created when the directory isn't plugged in.
        assert s.query(Employee).count() == 0
