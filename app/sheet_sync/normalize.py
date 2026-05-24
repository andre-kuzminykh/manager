"""Deterministic normalization + hashing for Google Sheets sync (spec §18.1).

Stdlib-only. Rules (must be deterministic across environments — NFR-GS-008):
  - trim strings; collapse internal whitespace is NOT applied (titles keep
    inner spacing), only outer .strip();
  - combine (date, time) into a tz-aware datetime → UTC ISO 8601 string;
  - empty optional → None;
  - hash is sha256 over JSON with sorted keys + compact separators.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date as _date, datetime, time as _time
from zoneinfo import ZoneInfo


class DateTimeError(ValueError):
    """Raised when a time is given without a date (spec §20.4: error)."""


def norm_str(value: str | None) -> str | None:
    """Trim; empty → None."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


_DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y")
_TIME_FORMATS = ("%H:%M", "%H:%M:%S", "%H.%M")


def _parse_date(s: str) -> _date:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unparseable date: {s!r}")


def _parse_time(s: str) -> _time:
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"unparseable time: {s!r}")


def combine_datetime(
    date_str: str | None, time_str: str | None, *, tz: str
) -> str | None:
    """Combine a date + optional time in `tz` → UTC ISO 8601 string.

    spec §20.4:
      - time without date  → DateTimeError;
      - date without time  → allowed, time defaults to 00:00;
      - both empty         → None.
    """
    d = norm_str(date_str)
    t = norm_str(time_str)
    if not d:
        if t:
            raise DateTimeError("time provided without a date")
        return None
    day = _parse_date(d)
    clock = _parse_time(t) if t else _time(0, 0)
    aware = datetime.combine(day, clock, tzinfo=ZoneInfo(tz))
    return aware.astimezone(ZoneInfo("UTC")).isoformat()


def stable_hash(payload: dict, *, fields: tuple[str, ...] | None = None) -> str:
    """sha256 over a deterministic JSON projection of `payload`.

    If `fields` is given, only those keys are hashed (sorted, missing → null),
    so the hash is stable regardless of extra display-only keys.
    """
    if fields is not None:
        subset = {k: payload.get(k) for k in fields}
    else:
        subset = payload
    blob = json.dumps(subset, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


__all__ = ["DateTimeError", "norm_str", "combine_datetime", "stable_hash"]
