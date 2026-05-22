"""FR-CB2-2.6 — combined JSONL + PG archive write with PG-fail
fallback into a pending JSONL that a cron job replays later.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.ceo_brain.archive import jsonl_sink, pg_sink
from app.logging_setup import get_logger

log = get_logger(__name__)


def write_archive(
    *,
    archive_dir: Path | str,
    pg_session: Session | None,
    channel_id: str,
    ts: str,
    text: str | None = None,
    channel_name: str | None = None,
    thread_ts: str | None = None,
    user_id: str | None = None,
    user_display_name: str | None = None,
    subtype: str | None = None,
    raw_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """JSONL is ALWAYS attempted. PG is attempted only when a
    session is provided; on PG-fail we drop a pending-row so a
    cron job can replay later.

    Returns ``{"jsonl_path": Path, "pg_row_id": int|None, "pending": bool}``.
    """
    payload = {
        "channel_name": channel_name,
        "thread_ts": thread_ts,
        "user": user_id,
        "user_display_name": user_display_name,
        "subtype": subtype,
        "text": text,
        "raw_payload": raw_payload or {},
    }

    jsonl_path = jsonl_sink.write(
        archive_dir=archive_dir,
        channel_id=channel_id,
        channel_name=channel_name,
        ts=ts,
        payload=payload,
    )

    pg_row_id: int | None = None
    pending = False
    if pg_session is not None:
        try:
            row = pg_sink.write(
                pg_session,
                channel_id=channel_id,
                ts=ts,
                text=text,
                raw_payload=raw_payload,
                channel_name=channel_name,
                thread_ts=thread_ts,
                user_id=user_id,
                user_display_name=user_display_name,
                subtype=subtype,
            )
            pg_row_id = row.id if row is not None else None
        except SQLAlchemyError as e:
            # FR-CR-05-192v polish #2 — when pg_sink.write raises
            # (UniqueViolation from race with history poller, etc),
            # the SQLAlchemy session is left in an aborted state.
            # Subsequent operations on the same session (classify_and_persist,
            # responder DB queries) will crash with PendingRollbackError.
            # Rollback HERE so downstream callers receive a usable session.
            try:
                pg_session.rollback()
            except Exception:  # noqa: BLE001
                pass
            log.warning(
                "brain_archive_pg_failed",
                error=str(e),
                hint=(
                    "session.rollback() applied so downstream code "
                    "(classify_and_persist, responder) gets a clean "
                    "transaction"
                ),
            )
            jsonl_sink.write_pending(
                archive_dir=archive_dir,
                record={
                    "channel_id": channel_id,
                    "ts": ts,
                    "text": text,
                    "channel_name": channel_name,
                    "thread_ts": thread_ts,
                    "user_id": user_id,
                    "user_display_name": user_display_name,
                    "subtype": subtype,
                    "raw_payload": raw_payload or {},
                },
            )
            pending = True
    else:
        # No session passed in (e.g. config explicitly disabled
        # PG, or PG is down on connect). Push to pending for
        # future replay.
        jsonl_sink.write_pending(
            archive_dir=archive_dir,
            record={
                "channel_id": channel_id,
                "ts": ts,
                "text": text,
                "channel_name": channel_name,
                "thread_ts": thread_ts,
                "user_id": user_id,
                "user_display_name": user_display_name,
                "subtype": subtype,
                "raw_payload": raw_payload or {},
            },
        )
        pending = True

    return {
        "jsonl_path": jsonl_path,
        "pg_row_id": pg_row_id,
        "pending": pending,
    }


__all__ = ["write_archive"]
