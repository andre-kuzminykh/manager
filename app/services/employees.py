"""Employees directory — synced from Slack ``users.info`` on first sight
and refreshed at most once per ``EMPLOYEE_REFRESH_TTL_SECONDS``.

Admins are derived from the ADMIN_SLACK_USER_IDS env var, with
``Employee.is_admin`` kept in sync on every upsert.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.logging_setup import get_logger
from app.models import Employee

log = get_logger(__name__)


def admin_slack_user_ids(settings: Settings | None = None) -> set[str]:
    settings = settings or get_settings()
    raw = (settings.admin_slack_user_ids or "").strip()
    if not raw:
        return set()
    return {p.strip() for p in raw.split(",") if p.strip()}


def is_admin(user_id: str | None, settings: Settings | None = None) -> bool:
    if not user_id:
        return False
    return user_id in admin_slack_user_ids(settings)


class EmployeeDirectory:
    """Upserts Employee rows from Slack. Callers pass a Slack ``WebClient``
    so we can issue ``users.info`` when a profile needs a refresh."""

    def __init__(
        self,
        *,
        client: WebClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._client = client
        self._settings = settings or get_settings()
        # FR-CR-04-22: per-process throttle for `ensure_channel_synced` so
        # that we don't hit `conversations.members` on every event. Maps
        # channel_id → last sync datetime.
        self._channel_sync_cache: dict[str, datetime] = {}

    def observed(
        self,
        session: Session,
        *,
        slack_user_id: str,
        seen_at: datetime | None = None,
    ) -> Employee:
        """Mark that we've seen this user and trigger a profile refresh if the
        cache is stale. Always returns an Employee row."""
        seen_at = seen_at or datetime.now(timezone.utc)
        employee = session.get(Employee, slack_user_id)
        created = False
        if employee is None:
            employee = Employee(
                slack_user_id=slack_user_id,
                is_admin=is_admin(slack_user_id, self._settings),
            )
            session.add(employee)
            session.flush()
            created = True

        # Refresh last_seen_at unconditionally (cheap DB write).
        employee.last_seen_at = seen_at

        # Flip admin bit to match config without waiting for a refresh.
        expected_admin = is_admin(slack_user_id, self._settings)
        if employee.is_admin != expected_admin:
            employee.is_admin = expected_admin

        if self._needs_refresh(employee, force=created):
            self._refresh_profile(session, employee)
        session.flush()
        return employee

    # ---- internals ----------------------------------------------------

    def _needs_refresh(self, employee: Employee, *, force: bool) -> bool:
        if self._client is None:
            return False
        if force or employee.profile_refreshed_at is None:
            return True
        ttl = timedelta(seconds=self._settings.employee_refresh_ttl_seconds)
        # Postgres returns tz-aware datetimes; SQLite may drop the tz. Normalise.
        refreshed = employee.profile_refreshed_at
        if refreshed.tzinfo is None:
            refreshed = refreshed.replace(tzinfo=timezone.utc)
        return refreshed + ttl <= datetime.now(timezone.utc)

    def _refresh_profile(self, session: Session, employee: Employee) -> None:
        assert self._client is not None
        try:
            resp = self._client.users_info(user=employee.slack_user_id)
        except SlackApiError as e:
            log.warning(
                "users_info_failed",
                user=employee.slack_user_id,
                error=str(e),
            )
            return

        user = dict(resp.get("user") or {})
        if not user:
            return
        profile = dict(user.get("profile") or {})
        employee.team_id = user.get("team_id") or employee.team_id
        employee.display_name = (
            profile.get("display_name_normalized")
            or profile.get("display_name")
            or user.get("name")
            or employee.display_name
        )
        employee.real_name = (
            profile.get("real_name_normalized")
            or profile.get("real_name")
            or employee.real_name
        )
        employee.email = profile.get("email") or employee.email
        employee.title = profile.get("title") or employee.title
        employee.timezone = user.get("tz") or employee.timezone
        employee.is_bot = bool(user.get("is_bot", employee.is_bot))
        employee.profile_raw = user
        employee.profile_refreshed_at = datetime.now(timezone.utc)


    def sync_workspace_members(
        self, session: Session, *, page_size: int = 200
    ) -> int:
        """Pull every workspace member via Slack's `users.list` and upsert
        them into the Employees directory. Returns the number of rows
        touched (created + updated).

        Slack rate limits `users.list` at Tier-2 (~20 req/min). Pagination
        is handled via the cursor field. Bots / deleted users are kept
        but flagged so the LLM owner prompt can filter them out (it
        already does — bots are excluded).
        """
        if self._client is None:
            return 0
        touched = 0
        cursor: str | None = None
        while True:
            try:
                kwargs: dict[str, Any] = {"limit": page_size}
                if cursor:
                    kwargs["cursor"] = cursor
                resp = self._client.users_list(**kwargs)
            except SlackApiError as e:
                log.warning("users_list_failed", error=str(e))
                break
            for raw in resp.get("members") or []:
                if self._upsert_from_users_list(session, raw):
                    touched += 1
            cursor = (resp.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                break
        if touched:
            session.flush()
        log.info("employees_workspace_synced", touched=touched)
        return touched

    def sync_channel_members(
        self,
        session: Session,
        *,
        channel_id: str,
        page_size: int = 200,
    ) -> int:
        """Walk `conversations.members` for one channel and refresh the
        Employee row for each. Use this on `member_joined_channel` when
        the bot itself is the joiner — quick way to learn who's in the
        room without waiting for them to post.
        """
        if self._client is None:
            return 0
        touched = 0
        cursor: str | None = None
        while True:
            try:
                kwargs: dict[str, Any] = {
                    "channel": channel_id,
                    "limit": page_size,
                }
                if cursor:
                    kwargs["cursor"] = cursor
                resp = self._client.conversations_members(**kwargs)
            except SlackApiError as e:
                log.warning(
                    "conversations_members_failed",
                    channel=channel_id,
                    error=str(e),
                )
                break
            for uid in resp.get("members") or []:
                self.observed(session, slack_user_id=uid)
                touched += 1
            cursor = (resp.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                break
        log.info(
            "employees_channel_synced", channel=channel_id, touched=touched
        )
        return touched

    def ensure_channel_synced(
        self,
        session: Session,
        *,
        channel_id: str,
        ttl_seconds: int = 1800,
    ) -> int:
        """Throttled wrapper around `sync_channel_members` (FR-CR-04-22).

        Slack passes us the conversation id on every event, so calling
        `sync_channel_members` from each handler would hammer
        `conversations.members`. This method caches the last sync time
        per channel in process memory and skips when the TTL hasn't
        elapsed. On first sight of a channel, sync runs — that fixes
        the long-standing "bot only knows people who have posted" gap
        without flooding the Slack API.
        """
        if self._client is None or not channel_id:
            return 0
        last = self._channel_sync_cache.get(channel_id)
        now = datetime.now(timezone.utc)
        if last is not None and (now - last).total_seconds() < ttl_seconds:
            return 0
        self._channel_sync_cache[channel_id] = now
        return self.sync_channel_members(session, channel_id=channel_id)

    def _upsert_from_users_list(
        self, session: Session, raw: dict[str, Any]
    ) -> bool:
        """Insert / update an Employee row from a `users.list` member.
        Returns True when the row was created or modified."""
        slack_user_id = raw.get("id")
        if not slack_user_id:
            return False
        # Skip the special slackbot user — it isn't a real teammate.
        if slack_user_id == "USLACKBOT":
            return False

        employee = session.get(Employee, slack_user_id)
        created = False
        if employee is None:
            employee = Employee(
                slack_user_id=slack_user_id,
                is_admin=is_admin(slack_user_id, self._settings),
            )
            session.add(employee)
            created = True

        profile = dict(raw.get("profile") or {})
        new_display = (
            profile.get("display_name_normalized")
            or profile.get("display_name")
            or raw.get("name")
            or employee.display_name
        )
        new_real = (
            profile.get("real_name_normalized")
            or profile.get("real_name")
            or employee.real_name
        )
        new_email = profile.get("email") or employee.email
        new_title = profile.get("title") or employee.title
        new_tz = raw.get("tz") or employee.timezone
        new_team = raw.get("team_id") or employee.team_id
        new_is_bot = bool(raw.get("is_bot", employee.is_bot))

        changed = (
            created
            or new_display != employee.display_name
            or new_real != employee.real_name
            or new_email != employee.email
            or new_title != employee.title
            or new_tz != employee.timezone
            or new_team != employee.team_id
            or new_is_bot != employee.is_bot
        )

        employee.display_name = new_display
        employee.real_name = new_real
        employee.email = new_email
        employee.title = new_title
        employee.timezone = new_tz
        employee.team_id = new_team
        employee.is_bot = new_is_bot
        employee.profile_raw = raw
        employee.profile_refreshed_at = datetime.now(timezone.utc)
        # Flip admin flag based on current config.
        employee.is_admin = is_admin(slack_user_id, self._settings)
        return changed


def sync_admin_flags(session: Session, settings: Settings | None = None) -> int:
    """Backfill: flip ``is_admin`` for any existing employee based on the
    current config. Returns the number of rows updated."""
    target = admin_slack_user_ids(settings)
    updated = 0
    for e in session.query(Employee).all():
        expected = e.slack_user_id in target
        if e.is_admin != expected:
            e.is_admin = expected
            updated += 1
    if updated:
        session.flush()
    return updated
