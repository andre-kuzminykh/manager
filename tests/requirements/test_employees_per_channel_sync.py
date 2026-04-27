"""FR-CR-04-22 — per-channel employee sync.

`EmployeeDirectory.ensure_channel_synced` walks
`conversations.members` for the conversation the bot was just
addressed in and upserts every member, so the owner LLM stage knows
about everyone in the room — not only people who have already posted.

Throttled in process memory: each channel hits Slack at most once per
TTL window (default 30 min)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import Employee
from app.services.employees import EmployeeDirectory


class _StubClient:
    def __init__(self, *, members_per_channel):
        self._members = members_per_channel
        self.calls: list[str] = []

    def conversations_members(self, *, channel, limit, cursor=None):  # noqa: D401
        self.calls.append(channel)
        return {
            "members": list(self._members.get(channel, [])),
            "response_metadata": {"next_cursor": ""},
        }

    # `observed()` calls users.info to refresh profiles. We stub it to
    # avoid blowing up when the directory tries to fill profile fields
    # for newly-discovered users.
    def users_info(self, user):  # noqa: D401
        return {"user": {"id": user, "team_id": "T1", "profile": {}}}


def test_ensure_channel_synced_seeds_employees_for_channel(session):
    client = _StubClient(members_per_channel={"C1": ["U-alice", "U-bob"]})
    directory = EmployeeDirectory(client=client)

    n = directory.ensure_channel_synced(session, channel_id="C1")
    assert n == 2

    sids = {e.slack_user_id for e in session.query(Employee).all()}
    assert sids == {"U-alice", "U-bob"}


def test_ensure_channel_synced_throttles_repeat_calls(session):
    client = _StubClient(members_per_channel={"C1": ["U-alice"]})
    directory = EmployeeDirectory(client=client)

    directory.ensure_channel_synced(session, channel_id="C1")
    directory.ensure_channel_synced(session, channel_id="C1")
    # Second call hit the cache — only one Slack call total.
    assert client.calls == ["C1"]


def test_ensure_channel_synced_runs_again_after_ttl(session):
    client = _StubClient(members_per_channel={"C1": ["U-alice"]})
    directory = EmployeeDirectory(client=client)

    directory.ensure_channel_synced(session, channel_id="C1")
    # Pretend the first call was an hour ago.
    directory._channel_sync_cache["C1"] = datetime.now(timezone.utc) - timedelta(
        seconds=3600
    )
    directory.ensure_channel_synced(session, channel_id="C1", ttl_seconds=1800)
    assert len(client.calls) == 2


def test_ensure_channel_synced_isolates_channels(session):
    client = _StubClient(
        members_per_channel={"C1": ["U-alice"], "C2": ["U-bob"]}
    )
    directory = EmployeeDirectory(client=client)

    directory.ensure_channel_synced(session, channel_id="C1")
    directory.ensure_channel_synced(session, channel_id="C2")
    assert client.calls == ["C1", "C2"]


def test_ensure_channel_synced_no_op_without_client(session):
    directory = EmployeeDirectory(client=None)
    assert directory.ensure_channel_synced(session, channel_id="C1") == 0
