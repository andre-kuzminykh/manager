"""Regression for the intent-classification bias.

The LLM kept choosing update_task for messages that clearly introduce a
new task. The prompt now spells out the distinction and gives a worked
example for date-table lookup too."""
from __future__ import annotations

from app.intent.prompts import SYSTEM_PROMPT


def test_prompt_describes_create_vs_update_task_bias():
    assert "introduces a new actionable task" in SYSTEM_PROMPT
    # Should prefer create_task as the default when ambiguous.
    assert "Prefer this over update_task" in SYSTEM_PROMPT
    # update_task requires a reference to an EXISTING task.
    assert "EXISTING task by reference" in SYSTEM_PROMPT


def test_prompt_has_worked_example_for_weekday_lookup():
    assert "Worked example" in SYSTEM_PROMPT
    assert "Monday → 2026-04-27" in SYSTEM_PROMPT
    assert 'due_date = "2026-04-27"' in SYSTEM_PROMPT


def test_prompt_makes_date_resolution_mandatory():
    assert "mandatory, not optional" in SYSTEM_PROMPT
    assert "Don't leave due_date null just because" in SYSTEM_PROMPT
