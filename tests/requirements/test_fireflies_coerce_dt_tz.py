"""Bug 2026-06-02 — Fireflies `_coerce_dt` must return tz-aware UTC.

Fireflies sends the meeting date as Unix-millis; the old millis branch used
`datetime.fromtimestamp(secs)` which returns a NAIVE local datetime. That broke
the calendar steps with «can't subtract offset-naive and offset-aware
datetimes» (observed in `fireflies_calendar_attendees_step_failed`), so a
meeting like «CE Ventures — питч Humanoid» never resolved attendees / matched
its calendar title. Every parse path must now yield aware-UTC.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.fireflies.client import _coerce_dt


def test_millis_int_returns_aware_utc():
    d = _coerce_dt(1780402800000)  # Fireflies Unix-millis
    assert d is not None and d.tzinfo is not None
    assert d.utcoffset() == timezone.utc.utcoffset(None)


def test_naive_datetime_input_is_assumed_utc():
    d = _coerce_dt(datetime(2026, 6, 2, 12, 5))  # naive
    assert d.tzinfo is not None
    assert (d.year, d.month, d.day, d.hour, d.minute) == (2026, 6, 2, 12, 5)


def test_iso_string_with_z_is_aware():
    d = _coerce_dt("2026-06-02T12:05:00Z")
    assert d is not None and d.tzinfo is not None


def test_aware_input_normalised_to_utc():
    from datetime import timedelta
    other = timezone(timedelta(hours=3))
    d = _coerce_dt(datetime(2026, 6, 2, 15, 5, tzinfo=other))  # 12:05 UTC
    assert d.tzinfo is not None
    assert (d.hour, d.minute) == (12, 5)


def test_none_passthrough():
    assert _coerce_dt(None) is None


def test_result_subtracts_cleanly_against_aware_event_start():
    """The actual crash: abs(ev_start - meeting_date) with mixed tz-awareness.
    With the fix, meeting_date is aware so the subtraction never raises."""
    meeting_date = _coerce_dt(1780402800000)
    ev_start = datetime(2026, 6, 2, 13, 0, tzinfo=timezone.utc)
    # must NOT raise «can't subtract offset-naive and offset-aware datetimes»
    delta = abs(ev_start - meeting_date)
    assert delta.total_seconds() >= 0
