"""FR-CR-05-185 — parse LLM-emitted `due_date` / `due_time` fields
from task extraction output into Python `date` / `time` objects.

The TASK_EXTRACTION_SYSTEM prompt now asks the LLM to resolve
relative deadlines («завтра», «в понедельник», «к концу недели»)
into absolute ISO `YYYY-MM-DD` form (against the meeting's
`today_date`). This module validates + coerces those strings,
falling back to today/18:00 when the model omits or emits
something we can't parse.

Operator-pinned 2026-05-21: «Meta - отправить напоминание завтра»
should land with `due_date=tomorrow`, not `due_date=today`.
"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from app.logging_setup import get_logger

log = get_logger(__name__)


def parse_due_date_from_llm(
    raw: Any,
    *,
    fallback: date,
) -> date:
    """Coerce an LLM-emitted `due_date` value into `datetime.date`.

    Accepts:
      - None / "" / missing → fallback (today)
      - ISO `YYYY-MM-DD`
      - ISO with time `YYYY-MM-DDTHH:MM:SS` (time stripped)
      - `datetime.date` / `datetime.datetime` instance

    On any parse failure → fallback (logged so operator can spot
    a model that ignored the contract).
    """
    if raw is None or raw == "":
        return fallback
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if not isinstance(raw, str):
        log.info(
            "task_due_date_unexpected_type",
            raw_type=type(raw).__name__, value=str(raw)[:50],
        )
        return fallback
    s = raw.strip()
    if not s:
        return fallback
    try:
        if "T" in s or " " in s and ":" in s:
            return datetime.fromisoformat(s.replace(" ", "T")).date()
        return date.fromisoformat(s)
    except (ValueError, TypeError):
        log.info(
            "task_due_date_unparseable",
            raw=s[:50], fallback=fallback.isoformat(),
        )
        return fallback


def parse_due_time_from_llm(
    raw: Any,
    *,
    fallback: time = time(23, 59),
) -> time:
    """Coerce an LLM-emitted `due_time` into `datetime.time`.

    Accepts `HH:MM` or `HH:MM:SS`. Anything else → fallback
    (23:59 — end of the deadline day, per FR-CR-05-210; was 18:00 in
    FR-CR-05-63).
    """
    if raw is None or raw == "":
        return fallback
    if isinstance(raw, time):
        return raw
    if not isinstance(raw, str):
        return fallback
    s = raw.strip()
    if not s:
        return fallback
    try:
        return time.fromisoformat(s)
    except (ValueError, TypeError):
        log.info(
            "task_due_time_unparseable",
            raw=s[:30], fallback=fallback.isoformat(),
        )
        return fallback


__all__ = [
    "parse_due_date_from_llm",
    "parse_due_time_from_llm",
]
