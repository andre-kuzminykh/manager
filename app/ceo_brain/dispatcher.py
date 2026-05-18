"""FR-CB2-1.x — Slack Events dispatcher.

Receives a payload from the Socket Mode client, applies dedup +
self-skip + archive whitelist filters, then dispatches to the
archive sink and (optionally) the responder pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.ceo_brain.archive import should_archive_channel, write_archive
from app.ceo_brain.cache import (
    resolve_channel_name,
    resolve_user_display_name,
)
from app.ceo_brain.config import get_archive_dir
from app.logging_setup import get_logger
from app.models import ProcessedSlackEvent

log = get_logger(__name__)


# Events that trigger the responder; everything else is archive-only.
_RESPONDER_EVENT_TYPES = {"app_mention"}


@dataclass
class DispatchResult:
    archived: bool = False
    duplicate: bool = False
    skipped_self: bool = False
    skipped_unsupported: bool = False
    responder_triggered: bool = False
    responder_error: str | None = None


def _is_self(payload: dict[str, Any], bot_user_id: str | None) -> bool:
    if not bot_user_id:
        return False
    return (payload.get("user") or "") == bot_user_id


def _is_duplicate(session: Session, event_id: str | None) -> bool:
    """Re-use the existing ``processed_slack_events`` table for
    event_id dedup. The table predates this feature but is
    structurally identical to what we need (UNIQUE(event_id))."""
    if not event_id:
        return False
    row = (
        session.query(ProcessedSlackEvent)
        .filter(ProcessedSlackEvent.event_id == event_id)
        .first()
    )
    return row is not None


def _mark_processed(session: Session, event_id: str | None) -> None:
    if not event_id:
        return
    session.add(ProcessedSlackEvent(event_id=event_id))
    session.flush()


def _channel_type(payload: dict[str, Any]) -> str | None:
    """Best-effort channel type. Slack Events sends `channel_type`
    on most message events (`channel` / `group` / `im` / `mpim`)."""
    return payload.get("channel_type") or payload.get("channelType")


def handle_event(
    session: Session,
    payload: dict[str, Any],
    *,
    bot_user_id: str | None = None,
    responder: Callable[[dict[str, Any]], None] | None = None,
    archive_dir: Any = None,
) -> DispatchResult:
    """FR-CB2-1.5/1.6 + FR-CB2-2.x + FR-CB2-3.1/3.2/3.3 — single
    entry point per Slack event.

    Returns a `DispatchResult` with flags describing what happened.
    """
    result = DispatchResult()
    if not isinstance(payload, dict):
        result.skipped_unsupported = True
        return result

    event_type = payload.get("type") or ""
    subtype = payload.get("subtype")

    # FR-CB2-1.6 — anti-self loop.
    if _is_self(payload, bot_user_id) or payload.get("bot_id") and (
        payload.get("user") == bot_user_id
    ):
        result.skipped_self = True
        return result

    # FR-CB2-1.5 — dedup by event_id.
    event_id = payload.get("event_id")
    if event_id and _is_duplicate(session, event_id):
        result.duplicate = True
        return result

    channel_id = payload.get("channel") or ""
    ts = payload.get("ts") or payload.get("event_ts") or ""

    archive_supported = event_type in {
        "message", "app_mention",
    } or event_type == ""  # bare-Events test payloads
    archive_dir = archive_dir or get_archive_dir()

    if archive_supported and channel_id and ts and should_archive_channel(channel_id):
        channel_name = resolve_channel_name(channel_id) or None
        user_id = payload.get("user") or None
        user_display = resolve_user_display_name(user_id) if user_id else None
        write_archive(
            archive_dir=archive_dir,
            pg_session=session,
            channel_id=channel_id,
            channel_name=channel_name,
            ts=ts,
            thread_ts=payload.get("thread_ts"),
            user_id=user_id,
            user_display_name=user_display,
            subtype=subtype,
            text=payload.get("text"),
            raw_payload=payload,
        )
        result.archived = True

    if event_id:
        try:
            _mark_processed(session, event_id)
        except Exception as e:  # noqa: BLE001
            log.warning("brain_dedup_mark_failed", error=str(e))

    # Responder dispatch — only on @mention / DM, and only if a
    # responder callable was passed in (Sprint 1 starts archive-only).
    if responder is not None:
        if event_type == "app_mention":
            result.responder_triggered = True
            try:
                responder(payload)
            except Exception as e:  # noqa: BLE001
                result.responder_error = str(e)
                log.warning(
                    "brain_responder_invocation_failed",
                    error=str(e),
                )
        elif event_type == "message" and _channel_type(payload) == "im":
            result.responder_triggered = True
            try:
                responder(payload)
            except Exception as e:  # noqa: BLE001
                result.responder_error = str(e)
                log.warning(
                    "brain_responder_invocation_failed",
                    error=str(e),
                )

    return result


__all__ = ["DispatchResult", "handle_event"]
