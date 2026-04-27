"""Requirement coverage: FR-CR-04-17 (workspace-wide employees sync).

Bot keeps the Employees directory complete by:
- pulling `users.list` on startup / on demand (`sync_workspace_members`)
- pulling `conversations.members` when bot joins a channel (`sync_channel_members`)
- listening for `team_join` and `member_joined_channel` events.

Tests below mock the Slack client and assert the upsert behaviour."""
from __future__ import annotations

from unittest.mock import MagicMock

from app.models import Employee
from app.services.employees import EmployeeDirectory


def _users_list_response(members, next_cursor=""):
    resp = {
        "members": members,
        "response_metadata": {"next_cursor": next_cursor},
    }
    return resp


def _conv_members_response(uids, next_cursor=""):
    return {
        "members": uids,
        "response_metadata": {"next_cursor": next_cursor},
    }


def _user_payload(uid, *, name="someone", real="Someone Real", is_bot=False):
    return {
        "id": uid,
        "team_id": "T1",
        "name": name,
        "is_bot": is_bot,
        "tz": "Europe/London",
        "profile": {
            "display_name": name,
            "real_name": real,
            "email": f"{name}@example.com",
            "title": "engineer",
        },
    }


# --------------------------------------------------------------------------- #
# users.list pagination + upsert
# --------------------------------------------------------------------------- #


def test_sync_workspace_members_inserts_new_rows(
    patched_session_scope, SessionFactory
):
    client = MagicMock()
    client.users_list.return_value = _users_list_response(
        [
            _user_payload("U1", name="andre", real="Andre Kuzminykh"),
            _user_payload("U2", name="ivan", real="Ivan Petrov"),
        ]
    )
    directory = EmployeeDirectory(client=client)
    with SessionFactory() as s:
        touched = directory.sync_workspace_members(s)
        s.commit()

    assert touched == 2
    with SessionFactory() as s:
        rows = sorted(s.query(Employee).all(), key=lambda e: e.slack_user_id)
        assert [e.slack_user_id for e in rows] == ["U1", "U2"]
        assert rows[0].real_name == "Andre Kuzminykh"
        assert rows[0].email == "andre@example.com"
        assert rows[1].display_name == "ivan"


def test_sync_workspace_members_updates_existing_row(
    patched_session_scope, SessionFactory
):
    with SessionFactory() as s:
        s.add(
            Employee(
                slack_user_id="U1",
                display_name="andre-old",
                real_name="Andre Old",
            )
        )
        s.commit()

    client = MagicMock()
    client.users_list.return_value = _users_list_response(
        [_user_payload("U1", name="andre-new", real="Andre Kuzminykh")]
    )
    directory = EmployeeDirectory(client=client)
    with SessionFactory() as s:
        touched = directory.sync_workspace_members(s)
        s.commit()
    assert touched == 1
    with SessionFactory() as s:
        row = s.get(Employee, "U1")
        assert row.display_name == "andre-new"
        assert row.real_name == "Andre Kuzminykh"


def test_sync_workspace_members_pages_through_cursor(
    patched_session_scope, SessionFactory
):
    client = MagicMock()
    client.users_list.side_effect = [
        _users_list_response([_user_payload("U1")], next_cursor="cur1"),
        _users_list_response([_user_payload("U2")], next_cursor=""),
    ]
    directory = EmployeeDirectory(client=client)
    with SessionFactory() as s:
        directory.sync_workspace_members(s, page_size=1)
        s.commit()

    # Two API pages requested.
    assert client.users_list.call_count == 2
    second_call = client.users_list.call_args_list[1]
    assert second_call.kwargs.get("cursor") == "cur1"
    with SessionFactory() as s:
        ids = {e.slack_user_id for e in s.query(Employee).all()}
        assert ids == {"U1", "U2"}


def test_sync_workspace_members_skips_slackbot(
    patched_session_scope, SessionFactory
):
    """USLACKBOT is the special workspace bot; we never want it as an
    assignee candidate."""
    client = MagicMock()
    client.users_list.return_value = _users_list_response(
        [
            _user_payload("USLACKBOT", name="slackbot"),
            _user_payload("U1", name="andre"),
        ]
    )
    directory = EmployeeDirectory(client=client)
    with SessionFactory() as s:
        directory.sync_workspace_members(s)
        s.commit()
    with SessionFactory() as s:
        ids = {e.slack_user_id for e in s.query(Employee).all()}
        assert ids == {"U1"}


def test_sync_workspace_members_keeps_bots_with_flag(
    patched_session_scope, SessionFactory
):
    """Regular bot accounts are still ingested but flagged is_bot=True
    — the owner prompt filters them by that flag."""
    client = MagicMock()
    client.users_list.return_value = _users_list_response(
        [_user_payload("UBOT1", name="appbot", is_bot=True)]
    )
    directory = EmployeeDirectory(client=client)
    with SessionFactory() as s:
        directory.sync_workspace_members(s)
        s.commit()
    with SessionFactory() as s:
        row = s.get(Employee, "UBOT1")
        assert row is not None
        assert row.is_bot is True


def test_sync_workspace_members_swallows_api_error(
    patched_session_scope, SessionFactory
):
    from slack_sdk.errors import SlackApiError

    client = MagicMock()
    client.users_list.side_effect = SlackApiError(
        "rate limited", response=MagicMock(status_code=429)
    )
    directory = EmployeeDirectory(client=client)
    with SessionFactory() as s:
        # Must not raise.
        touched = directory.sync_workspace_members(s)
    assert touched == 0


# --------------------------------------------------------------------------- #
# conversations.members
# --------------------------------------------------------------------------- #


def test_sync_channel_members_observes_each_user(
    patched_session_scope, SessionFactory
):
    client = MagicMock()
    client.conversations_members.return_value = _conv_members_response(
        ["U1", "U2", "U3"]
    )
    client.users_info.side_effect = [
        {"user": _user_payload(uid, name=f"u{uid}")} for uid in ("U1", "U2", "U3")
    ]
    directory = EmployeeDirectory(client=client)
    with SessionFactory() as s:
        touched = directory.sync_channel_members(s, channel_id="C-test")
        s.commit()
    assert touched == 3
    with SessionFactory() as s:
        ids = {e.slack_user_id for e in s.query(Employee).all()}
        assert ids == {"U1", "U2", "U3"}


def test_sync_channel_members_pages_through_cursor(
    patched_session_scope, SessionFactory
):
    client = MagicMock()
    client.conversations_members.side_effect = [
        _conv_members_response(["U1"], next_cursor="x"),
        _conv_members_response(["U2"], next_cursor=""),
    ]
    client.users_info.side_effect = [
        {"user": _user_payload(uid)} for uid in ("U1", "U2")
    ]
    directory = EmployeeDirectory(client=client)
    with SessionFactory() as s:
        directory.sync_channel_members(s, channel_id="C-x")
        s.commit()
    assert client.conversations_members.call_count == 2
