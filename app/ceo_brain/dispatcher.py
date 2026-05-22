"""FR-CB2-1.x — Slack Events dispatcher.

Receives a payload from the Socket Mode client, applies dedup +
self-skip + archive whitelist filters, then dispatches to the
archive sink and (optionally) the responder pipeline.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.ceo_brain.archive import should_archive_channel, write_archive
from app.ceo_brain.archive.pg_sink import _find as _find_archive_row
from app.ceo_brain.cache import (
    resolve_channel_name,
    resolve_user_display_name,
)
from app.ceo_brain.config import get_archive_dir
from app.logging_setup import get_logger

log = get_logger(__name__)


# Events that trigger the responder; everything else is archive-only.
_RESPONDER_EVENT_TYPES = {"app_mention"}


# FR-CB2-1.8 — in-process responder dedup. Even when archive's
# UNIQUE(channel, ts) misses due to a race (Socket-Mode push +
# history-poller hitting the same message before either commits),
# this set keeps the responder strictly single-shot per (channel, ts)
# within the TTL window.
_RESPONDER_DEDUP_TTL_SEC = 60.0
_responder_dedup_lock = threading.Lock()
_responder_dedup: dict[tuple[str, str], float] = {}


def _prune_responder_dedup_locked() -> None:
    cutoff = time.monotonic() - _RESPONDER_DEDUP_TTL_SEC
    stale = [k for k, t in _responder_dedup.items() if t < cutoff]
    for k in stale:
        _responder_dedup.pop(k, None)


def _mark_responder_dispatched(channel_id: str, ts: str) -> bool:
    """Record that the responder fired for ``(channel_id, ts)``.
    Returns True if this is the first time (caller should fire),
    False if it's a duplicate (caller should skip)."""
    if not channel_id or not ts:
        return True
    key = (channel_id, ts)
    with _responder_dedup_lock:
        _prune_responder_dedup_locked()
        if key in _responder_dedup:
            return False
        _responder_dedup[key] = time.monotonic()
        return True


def _reset_responder_dedup_for_tests() -> None:
    with _responder_dedup_lock:
        _responder_dedup.clear()


@dataclass
class DispatchResult:
    archived: bool = False
    archive_failed: bool = False  # FR-CR-05-192v — best-effort archive
    duplicate: bool = False
    skipped_self: bool = False
    skipped_unsupported: bool = False
    responder_triggered: bool = False
    responder_error: str | None = None


def _is_self(payload: dict[str, Any], bot_user_id: str | None) -> bool:
    if not bot_user_id:
        return False
    return (payload.get("user") or "") == bot_user_id


def _is_duplicate_archive(
    session: Session, *, channel_id: str, ts: str,
) -> bool:
    """Dedup using the archive's own UNIQUE(channel_id, ts) — this
    keeps CEO Brain's dedup state independent of the existing
    slack-task-bot's ``processed_slack_events`` flow."""
    if not channel_id or not ts:
        return False
    return _find_archive_row(
        session, channel_id=channel_id, ts=ts,
    ) is not None


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

    channel_id = payload.get("channel") or ""
    ts = payload.get("ts") or payload.get("event_ts") or ""

    # FR-CB2-1.5 — dedup by archive's UNIQUE(channel_id, ts).
    if _is_duplicate_archive(session, channel_id=channel_id, ts=ts):
        result.duplicate = True
        return result

    archive_supported = event_type in {
        "message", "app_mention",
    } or event_type == ""  # bare-Events test payloads
    archive_dir = archive_dir or get_archive_dir()

    if archive_supported and channel_id and ts and should_archive_channel(channel_id):
        channel_name = resolve_channel_name(channel_id) or None
        user_id = payload.get("user") or None
        user_display = resolve_user_display_name(user_id) if user_id else None
        # FR-CR-05-192v — archive write MUST be best-effort. A
        # PermissionError / disk-full / other I/O failure on the
        # JSONL sink had been crashing the whole dispatcher,
        # which meant the responder never fired and the bot
        # appeared dead in DMs / threads. Audit trail is not as
        # important as the live agent — log the failure and
        # continue.
        try:
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
        except Exception as e:  # noqa: BLE001
            result.archive_failed = True
            from app.logging_setup import get_logger as _gl
            _gl(__name__).warning(
                "ceo_brain_archive_write_failed",
                channel_id=channel_id, ts=ts, error=str(e),
                hint=(
                    "responder will still fire — archive is "
                    "best-effort. Common causes: permission "
                    "denied on /app/traces, disk full."
                ),
            )

    # Responder dispatch — only on @mention or plain DM. Skip
    # `message.changed`/`message.deleted` (no `subtype` shape
    # means user-typed message; any `subtype` set means it's an
    # edit, deletion, channel-join, bot-relay, etc. — never a
    # responder trigger).
    if responder is not None:
        # FR-CB2-3.36 — operator-pinned whitelist. When set, only
        # listed Slack user IDs may invoke the responder; others
        # are silently ignored (no «access denied» reply).
        from app.config import get_settings
        allowed_raw = (
            get_settings().ceo_brain_allowed_users or ""
        ).strip()
        allowed_set: set[str] = set()
        if allowed_raw:
            allowed_set = {
                u.strip() for u in allowed_raw.split(",")
                if u and u.strip()
            }
        if allowed_set:
            sender = (payload.get("user") or "").strip()
            if sender not in allowed_set:
                log.info(
                    "ceo_brain_responder_user_not_allowed",
                    user=sender,
                )
                return result
        is_user_authored_message = (
            event_type == "message" and not subtype
        )
        should_fire = (
            event_type == "app_mention"
            or (is_user_authored_message and _channel_type(payload) == "im")
        )
        if should_fire:
            # FR-CB2-1.8 — in-process dedup, races past archive UNIQUE.
            if not _mark_responder_dispatched(channel_id, ts):
                log.info(
                    "ceo_brain_responder_in_process_dedup_skipped",
                    channel=channel_id, ts=ts,
                )
            else:
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


__all__ = [
    "DispatchResult",
    "handle_event",
    "_mark_responder_dispatched",
    "_reset_responder_dedup_for_tests",
]
