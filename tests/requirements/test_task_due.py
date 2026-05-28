"""FR-CR-05-185 — parse LLM-emitted `due_date` / `due_time` fields
into Python `date` / `time` objects.

Pins the contract so a future prompt change can't silently break the
deadline-extraction path without the suite catching it.
"""
from datetime import date, datetime, time

import pytest

from app.services.task_due import (
    parse_due_date_from_llm,
    parse_due_time_from_llm,
)


_TODAY = date(2026, 5, 21)


def test_due_date_none_returns_fallback():
    assert parse_due_date_from_llm(None, fallback=_TODAY) == _TODAY


def test_due_date_empty_string_returns_fallback():
    assert parse_due_date_from_llm("", fallback=_TODAY) == _TODAY


def test_due_date_iso_yyyy_mm_dd_parsed():
    assert parse_due_date_from_llm(
        "2026-05-26", fallback=_TODAY,
    ) == date(2026, 5, 26)


def test_due_date_iso_with_time_strips_time():
    assert parse_due_date_from_llm(
        "2026-05-26T18:00:00", fallback=_TODAY,
    ) == date(2026, 5, 26)


def test_due_date_passes_through_date_instance():
    d = date(2026, 5, 26)
    assert parse_due_date_from_llm(d, fallback=_TODAY) is d


def test_due_date_passes_through_datetime_instance():
    dt = datetime(2026, 5, 26, 18, 0)
    assert parse_due_date_from_llm(dt, fallback=_TODAY) == date(2026, 5, 26)


def test_due_date_garbage_returns_fallback():
    assert parse_due_date_from_llm(
        "завтра", fallback=_TODAY,
    ) == _TODAY
    assert parse_due_date_from_llm(
        "not a date", fallback=_TODAY,
    ) == _TODAY


def test_due_date_wrong_type_returns_fallback():
    assert parse_due_date_from_llm(
        12345, fallback=_TODAY,
    ) == _TODAY
    assert parse_due_date_from_llm(
        [], fallback=_TODAY,
    ) == _TODAY


def test_due_time_default_is_23_59():
    # FR-CR-05-210 — end-of-day default (was 18:00 in FR-CR-05-63).
    assert parse_due_time_from_llm(None) == time(23, 59)


def test_due_time_hh_mm_parsed():
    assert parse_due_time_from_llm("09:30") == time(9, 30)


def test_due_time_hh_mm_ss_parsed():
    assert parse_due_time_from_llm("09:30:00") == time(9, 30, 0)


def test_due_time_passes_through_time_instance():
    t = time(11, 15)
    assert parse_due_time_from_llm(t) is t


def test_due_time_garbage_returns_fallback():
    assert parse_due_time_from_llm("к утру") == time(23, 59)
    assert parse_due_time_from_llm("noon") == time(23, 59)


def test_due_time_custom_fallback():
    fb = time(9, 0)
    assert parse_due_time_from_llm("", fallback=fb) == fb
