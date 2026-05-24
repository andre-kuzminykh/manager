"""FR-GS-* / FR-TASK-* — deterministic core of Google Sheets Versioned Sync.

Covers (DB-free, network-free): normalization + hashing determinism
(NFR-GS-008), date/time rules (spec §20.4), task validation (UC-TASK-002..006,
FR-TASK-002/004/007/008/009/010), and the fallback task_signature (spec §20.5).
"""
from __future__ import annotations

import pytest

from app.sheet_sync.config import HASHABLE_FIELDS
from app.sheet_sync.identity import task_signature
from app.sheet_sync.normalize import (
    DateTimeError,
    combine_datetime,
    norm_str,
    stable_hash,
)
from app.sheet_sync.validation import validate_task_row


# resolver helpers --------------------------------------------------------
def _resolver(table: dict[str, object]):
    def resolve(name: str):
        v = table.get(name.strip())
        if v is None:
            return ("unknown", None)
        if v == "AMBIGUOUS":
            return ("ambiguous", None)
        return ("ok", v)
    return resolve


_TEAM = _resolver({"Anna Petrova": 7, "Dup Name": "AMBIGUOUS"})


def _row(**kw) -> dict:
    base = {h: "" for h in (
        "title", "description", "responsible", "status", "priority", "category",
        "start_date", "start_time", "deadline_date", "deadline_time",
        "completed_date", "completed_time", "comments",
    )}
    base.update(kw)
    return base


# --- normalize -----------------------------------------------------------
def test_norm_str_trims_and_empty_to_none():
    assert norm_str("  x  ") == "x"
    assert norm_str("   ") is None
    assert norm_str(None) is None


def test_combine_datetime_date_without_time_defaults_midnight_utc():
    # London 2026-06-01 (BST = UTC+1) midnight → 2026-05-31T23:00:00+00:00
    out = combine_datetime("2026-06-01", "", tz="Europe/London")
    assert out == "2026-05-31T23:00:00+00:00"


def test_combine_datetime_time_without_date_raises():
    with pytest.raises(DateTimeError):
        combine_datetime("", "10:00", tz="UTC")


def test_combine_datetime_both_empty_is_none():
    assert combine_datetime("", "", tz="UTC") is None


def test_combine_datetime_accepts_multiple_date_formats():
    a = combine_datetime("2026-06-01", "09:00", tz="UTC")
    b = combine_datetime("01.06.2026", "09:00", tz="UTC")
    c = combine_datetime("01/06/2026", "09:00", tz="UTC")
    assert a == b == c == "2026-06-01T09:00:00+00:00"


def test_stable_hash_deterministic_and_key_order_independent():
    h1 = stable_hash({"a": 1, "b": 2})
    h2 = stable_hash({"b": 2, "a": 1})
    assert h1 == h2 and h1.startswith("sha256:")


def test_stable_hash_subset_ignores_extra_keys():
    base = {"title": "x", "status": "todo", "assignee_name": "Anna"}
    more = {**base, "assignee_name": "DIFFERENT DISPLAY"}
    # assignee_name is NOT in HASHABLE_FIELDS → hash stays equal
    assert stable_hash(base, fields=HASHABLE_FIELDS) == stable_hash(more, fields=HASHABLE_FIELDS)


# --- validation ----------------------------------------------------------
def test_valid_task_row_builds_payload_hash_signature():
    r = validate_task_row(
        _row(title="Prepare launch plan", status="To Do", priority="High",
             responsible="Anna Petrova", category="Product",
             deadline_date="2026-06-10", deadline_time="18:00"),
        resolve_assignee=_TEAM, tz="UTC",
    )
    assert r.ok
    assert r.payload["status"] == "todo" and r.payload["priority"] == "high"
    assert r.payload["assignee_id"] == 7
    assert r.payload["deadline_at"] == "2026-06-10T18:00:00+00:00"
    assert r.payload_hash and r.signature


def test_empty_row_flagged_empty_no_errors():
    r = validate_task_row(_row(), resolve_assignee=_TEAM)
    assert r.is_empty and not r.errors and r.payload is None


def test_missing_title_rejected():
    r = validate_task_row(_row(status="To Do", priority="High"), resolve_assignee=_TEAM)
    assert not r.ok and any(e["error_type"] == "missing_title" for e in r.errors)
    assert r.payload is None  # no mutation


def test_invalid_priority_rejected():
    r = validate_task_row(
        _row(title="x", status="To Do", priority="Urgent"), resolve_assignee=_TEAM,
    )
    assert any(e["error_type"] == "invalid_priority" for e in r.errors)


def test_invalid_status_rejected():
    r = validate_task_row(
        _row(title="x", status="Wibble", priority="Low"), resolve_assignee=_TEAM,
    )
    assert any(e["error_type"] == "invalid_status" for e in r.errors)


def test_unknown_responsible_rejected():
    r = validate_task_row(
        _row(title="x", status="Done", priority="Low", responsible="Unknown Person",
             completed_date="2026-06-01"),
        resolve_assignee=_TEAM,
    )
    assert any(e["error_type"] == "unknown_responsible" for e in r.errors)
    assert r.payload is None


def test_ambiguous_responsible_rejected():
    r = validate_task_row(
        _row(title="x", status="To Do", priority="Low", responsible="Dup Name"),
        resolve_assignee=_TEAM,
    )
    assert any(e["error_type"] == "ambiguous_responsible" for e in r.errors)


def test_time_without_date_is_error():
    r = validate_task_row(
        _row(title="x", status="To Do", priority="Low", start_time="10:00"),
        resolve_assignee=_TEAM,
    )
    assert any(e["error_type"] == "invalid_start_datetime" for e in r.errors)


def test_done_without_completion_is_warning_not_error():
    r = validate_task_row(
        _row(title="x", status="Done", priority="Low"), resolve_assignee=_TEAM,
    )
    assert r.ok  # warning does not block
    assert any(w["error_type"] == "done_without_completion" for w in r.warnings)


def test_deadline_before_start_is_warning_not_error():
    r = validate_task_row(
        _row(title="x", status="To Do", priority="Low",
             start_date="2026-06-10", deadline_date="2026-06-01"),
        resolve_assignee=_TEAM,
    )
    assert r.ok
    assert any(w["error_type"] == "deadline_before_start" for w in r.warnings)


def test_no_op_edit_same_hash():
    args = dict(title="x", status="To Do", priority="High", responsible="Anna Petrova")
    r1 = validate_task_row(_row(**args), resolve_assignee=_TEAM)
    r2 = validate_task_row(_row(**args), resolve_assignee=_TEAM)
    assert r1.payload_hash == r2.payload_hash  # idempotent → no new state (UC-GS-007)


def test_signature_changes_when_identity_field_changes():
    a = validate_task_row(_row(title="A", status="To Do", priority="Low"), resolve_assignee=_TEAM)
    b = validate_task_row(_row(title="B", status="To Do", priority="Low"), resolve_assignee=_TEAM)
    assert a.signature != b.signature


def test_clearing_optional_field_changes_hash():
    full = validate_task_row(
        _row(title="x", status="To Do", priority="Low", comments="note"), resolve_assignee=_TEAM,
    )
    cleared = validate_task_row(
        _row(title="x", status="To Do", priority="Low"), resolve_assignee=_TEAM,
    )
    assert full.payload_hash != cleared.payload_hash  # optional clear = new state (UC-GS-013)
