"""Requirement coverage: FR-CR-02-3 (chat.update on reply applies the
parsed fields to the Task row).

_apply_payload_to_task maps a parsed-reply dict onto a Task's columns.
Used by _handle_followup_reply after the user answers a follow-up
question. This test grid exercises every supported field plus the
defensive branches (invalid priority, malformed ISO, invalid int)."""
from __future__ import annotations

from datetime import date

import pytest

from app.models import Task
from app.models.task import TaskPriority, TaskStatus
from app.slack_bot.handlers.events import _apply_payload_to_task


def _task(**kw) -> Task:
    defaults = dict(
        title="t",
        status=TaskStatus.todo,
        owner_user_id="U-author",
        priority=TaskPriority.medium,
    )
    defaults.update(kw)
    return Task(**defaults)


def test_apply_updates_title_and_description():
    task = _task()
    _apply_payload_to_task(task, {"title": "new", "description": "details"})
    assert task.title == "new"
    assert task.description == "details"


def test_apply_owner_clears_assumed_flag():
    task = _task(extra={"owner_assumed": True})
    _apply_payload_to_task(task, {"owner_user_id": "U-ivan"})
    assert task.owner_user_id == "U-ivan"
    assert task.extra is None or "owner_assumed" not in task.extra


def test_apply_owner_display_name_field():
    task = _task()
    _apply_payload_to_task(task, {"owner_display_name": "Иван"})
    assert task.owner_display_name == "Иван"


def test_apply_priority_accepts_valid_value():
    task = _task(priority=TaskPriority.medium)
    _apply_payload_to_task(task, {"priority": "high"})
    assert task.priority == TaskPriority.high


def test_apply_priority_silently_ignores_invalid():
    task = _task(priority=TaskPriority.medium)
    _apply_payload_to_task(task, {"priority": "bogus"})
    # Unchanged — no exception, just ignore.
    assert task.priority == TaskPriority.medium


def test_apply_due_date_from_iso_string():
    task = _task()
    _apply_payload_to_task(task, {"due_date": "2026-05-12"})
    assert task.due_date == date(2026, 5, 12)


def test_apply_due_date_from_date_object():
    task = _task()
    _apply_payload_to_task(task, {"due_date": date(2026, 7, 4)})
    assert task.due_date == date(2026, 7, 4)


def test_apply_due_date_ignores_malformed_iso():
    task = _task(due_date=date(2026, 1, 1))
    _apply_payload_to_task(task, {"due_date": "not-a-date"})
    assert task.due_date == date(2026, 1, 1)


def test_apply_estimated_minutes_coerces_int():
    task = _task()
    _apply_payload_to_task(task, {"estimated_minutes": "90"})
    assert task.estimated_minutes == 90


def test_apply_estimated_minutes_ignores_garbage():
    task = _task(estimated_minutes=60)
    _apply_payload_to_task(task, {"estimated_minutes": "bogus"})
    assert task.estimated_minutes == 60


def test_apply_skips_empty_values():
    task = _task(title="keep")
    _apply_payload_to_task(task, {"title": "", "description": None, "participants": []})
    assert task.title == "keep"  # empty string is skipped


def test_apply_unknown_fields_are_noop():
    task = _task()
    _apply_payload_to_task(task, {"datetime_at": "whatever", "participants": ["a"]})
    # Future/meeting fields are accepted but do nothing on a Task row.
    # No exception is the assertion.
