"""FR-CR-05-258 — Postgres advisory locks to serialise per-meeting processing.

The always-on runner (`ops/zoom_fireflies_runner.py`) and manual ops
(`ops/republish_meeting.py`, `ops/send_one_zoom.py`) can both touch the SAME
recording at once. The runner's idempotency is bookmark-based with a TOCTOU
window (it reads `tasks_extracted`/`last_error` in one session, then processes
in another), so a manual run firing while the cron tick runs → the meeting is
processed twice → duplicate Google Docs / Slack posts / n8n webhook fires.

`try_meeting_lock(session, source, source_id)` takes a TRANSACTION-scoped
advisory lock (auto-released when the surrounding transaction commits/rolls
back — i.e. when the `session_scope()` that wraps `process_one` exits). It
returns True if acquired, False if another transaction already holds it; the
caller should then skip this recording and let the next tick / a later run pick
it up once the other holder is done.
"""
from __future__ import annotations

import zlib
from typing import Any

from sqlalchemy import text

from app.logging_setup import get_logger

log = get_logger(__name__)


def _lock_key(name: str) -> int:
    """Stable 64-bit-safe signed int from a string key. crc32 → 0..2^32-1,
    shifted into the signed range so it fits a Postgres bigint parameter."""
    return int(zlib.crc32(name.encode("utf-8"))) - 2 ** 31


def try_meeting_lock(session: Any, source: str, source_id: str) -> bool:
    """Acquire a transaction-scoped advisory lock for one meeting.

    Held until the surrounding transaction ends (commit/rollback). Returns
    True when acquired (caller may process), False when another transaction
    holds it (caller should skip + retry later). Best-effort: any error
    acquiring the lock returns True so a lock-infra hiccup never blocks
    legitimate processing.
    """
    try:
        got = session.execute(
            text("SELECT pg_try_advisory_xact_lock(:k)"),
            {"k": _lock_key(f"meeting:{source}:{source_id}")},
        ).scalar()
        return bool(got)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "pg_advisory_lock_error", source=source,
            source_id=source_id, error=str(e),
        )
        return True


__all__ = ["try_meeting_lock"]
