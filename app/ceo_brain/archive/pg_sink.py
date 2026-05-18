"""FR-CB2-2.2/2.3/2.4/2.5 — PG mirror of the Slack archive.

INSERT one row per message with ``UNIQUE(channel_id, ts)``.
Duplicates are silently dropped (idempotency for retried Slack
events). Edits update text + bump edit_count; deletes set
``deleted_at`` without dropping the row.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SlackMessageArchive


def _parse_day(ts: str | None) -> date:
    """Slack `ts` is a float string seconds-since-epoch (with
    microseconds after the dot). Truncate to UTC date."""
    try:
        if not ts:
            return datetime.now(timezone.utc).date()
        seconds = float(ts)
        return datetime.fromtimestamp(seconds, tz=timezone.utc).date()
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).date()


def _find(
    session: Session, *, channel_id: str, ts: str,
) -> SlackMessageArchive | None:
    stmt = select(SlackMessageArchive).where(
        SlackMessageArchive.channel_id == channel_id,
        SlackMessageArchive.ts == ts,
    )
    return session.execute(stmt).scalar_one_or_none()


def write(
    session: Session,
    *,
    channel_id: str,
    ts: str,
    text: str | None = None,
    raw_payload: dict[str, Any] | None = None,
    channel_name: str | None = None,
    thread_ts: str | None = None,
    user_id: str | None = None,
    user_display_name: str | None = None,
    subtype: str | None = None,
) -> SlackMessageArchive | None:
    """FR-CB2-2.2/2.3 — INSERT idempotent on (channel_id, ts)."""
    if not channel_id or not ts:
        return None
    existing = _find(session, channel_id=channel_id, ts=ts)
    if existing is not None:
        return existing
    row = SlackMessageArchive(
        channel_id=channel_id,
        channel_name=channel_name,
        ts=ts,
        thread_ts=thread_ts,
        user_id=user_id,
        user_display_name=user_display_name,
        subtype=subtype,
        text=text,
        raw_payload=raw_payload or {},
        edit_count=0,
        day=_parse_day(ts),
    )
    session.add(row)
    session.flush()
    return row


def apply_edit(
    session: Session,
    *,
    channel_id: str,
    ts: str,
    text: str | None,
    raw_payload: dict[str, Any] | None = None,
) -> SlackMessageArchive | None:
    """FR-CB2-2.4 — overwrite text + bump edit_count for the
    (channel_id, ts) row."""
    row = _find(session, channel_id=channel_id, ts=ts)
    if row is None:
        # Edit arrived before the original — store as new row so
        # we at least keep the latest body.
        return write(
            session,
            channel_id=channel_id,
            ts=ts,
            text=text,
            raw_payload=raw_payload,
            subtype="message_changed",
        )
    row.text = text
    if raw_payload is not None:
        row.raw_payload = raw_payload
    row.edit_count = (row.edit_count or 0) + 1
    session.add(row)
    session.flush()
    return row


def apply_delete(
    session: Session,
    *,
    channel_id: str,
    ts: str,
) -> SlackMessageArchive | None:
    """FR-CB2-2.5 — soft-delete: set ``deleted_at`` but keep row."""
    row = _find(session, channel_id=channel_id, ts=ts)
    if row is None:
        return None
    row.deleted_at = datetime.now(timezone.utc)
    session.add(row)
    session.flush()
    return row


__all__ = ["apply_delete", "apply_edit", "write"]
