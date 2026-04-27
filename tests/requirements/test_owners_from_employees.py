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
    assert by_id["U-alice"] == "Alice"
    assert by_id["U-bob"] == "Bob"


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
