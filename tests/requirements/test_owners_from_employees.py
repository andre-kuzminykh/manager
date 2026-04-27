"""FR-CR-04-24 — owner-picker pulls from the employees table.

The Edit modal's owner dropdown used to read from `ALLOWED_OWNERS`
env (a static JSON list). It now reads from the `employees` table
that's kept fresh by `EmployeeDirectory` (workspace + per-channel
sync), so newcomers show up without an env redeploy.

Falls back to the env list when the employees table is empty (handy
for tests and brand-new deploys before the first sync runs)."""
from __future__ import annotations

from app.models import Employee
from app.services.owners import list_known_owners


def test_list_known_owners_returns_employees(session):
    session.add_all(
        [
            Employee(slack_user_id="U-alice", display_name="Alice", real_name="Alice A."),
            Employee(slack_user_id="U-bob", display_name="Bob", real_name="Bob B."),
        ]
    )
    session.flush()

    out = list_known_owners(session)
    sids = {o["slack_user_id"] for o in out}
    assert sids == {"U-alice", "U-bob"}
    by_id = {o["slack_user_id"]: o["display_name"] for o in out}
    # FR-CR-04-24: real_name takes priority over display_name to avoid
    # the @username fallback (e.g. "admin") leaking into the picker.
    assert by_id["U-alice"] == "Alice A."
    assert by_id["U-bob"] == "Bob B."


def test_list_known_owners_falls_back_to_display_name_when_no_real_name(session):
    session.add(
        Employee(slack_user_id="U-x", display_name="DispOnly", real_name=None)
    )
    session.flush()

    out = list_known_owners(session)
    assert out[0]["display_name"] == "DispOnly"


def test_list_known_owners_excludes_bots(session):
    session.add_all(
        [
            Employee(slack_user_id="U-human", display_name="Human"),
            Employee(slack_user_id="U-bot", display_name="Bot", is_bot=True),
        ]
    )
    session.flush()

    out = list_known_owners(session)
    assert {o["slack_user_id"] for o in out} == {"U-human"}


def test_list_known_owners_uses_real_name_when_display_missing(session):
    session.add(
        Employee(slack_user_id="U-x", display_name=None, real_name="Real Name")
    )
    session.flush()

    out = list_known_owners(session)
    assert out[0]["display_name"] == "Real Name"


def test_list_known_owners_real_name_wins_over_username_display_name(session):
    """The classic case the user reported: Slack falls back to the
    @username when the user hasn't set a custom display name, so we
    end up with `display_name="admin"` and `real_name="Andre
    Kuzminykh"`. The picker must show the real name."""
    session.add(
        Employee(
            slack_user_id="U-andre",
            display_name="admin",
            real_name="Andre Kuzminykh",
        )
    )
    session.flush()

    out = list_known_owners(session)
    assert out[0]["display_name"] == "Andre Kuzminykh"


def test_list_known_owners_falls_back_to_env_when_empty(session, monkeypatch):
    monkeypatch.setenv(
        "ALLOWED_OWNERS",
        '[{"slack_user_id":"U-env","display_name":"FromEnv"}]',
    )
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        out = list_known_owners(session)  # session is empty
        assert out == [{"slack_user_id": "U-env", "display_name": "FromEnv"}]
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]
