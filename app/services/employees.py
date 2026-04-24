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
