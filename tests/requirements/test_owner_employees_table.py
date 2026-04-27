"""Requirement coverage: FR-CR-04-12 (employees table in owner prompt).

When a Slack message names an assignee by name ("Иван, сделай X" or
"на Пашу"), the owner LLM stage now sees a structured table of known
employees — slack_user_id, display_name, real_name — and picks the
matching id from there instead of returning a bare display_name. Plus
hallucination protection: an id NOT in the table is dropped, and
display_name → id resolution still runs as a deterministic fallback.

This fixes the bug where the bot was assigning tasks to the message
author even when another team member was named in the text.
"""
from __future__ import annotations

from datetime import date

from app.context.retriever import ContextWindow
from app.intent.detect_prompt import DETECT_TOOL_NAME
from app.intent.owner_prompt import (
    OWNER_TOOL_NAME,
    build_owner_user_prompt,
)
from app.intent.pipeline import run_pipeline
from app.intent.title_prompt import TITLE_TOOL_NAME
from app.intent.date_prompt import DATE_TOOL_NAME
from app.schemas.intent import IntentType


class _Backend:
    def __init__(self, *, detect, title=None, owner=None, date_=None):
        self.payloads = {
            DETECT_TOOL_NAME: detect,
            TITLE_TOOL_NAME: title or {"title": "stub"},
            OWNER_TOOL_NAME: owner,
            DATE_TOOL_NAME: date_ or {"due_date": None, "reasoning": "no"},
        }
        self.calls: list[dict] = []

    def extract_intent(self, *, user_prompt):  # pragma: no cover
        raise NotImplementedError

    def call_tool(self, **kw):
        self.calls.append(kw)
        return self.payloads.get(kw["tool_name"])


# --------------------------------------------------------------------------- #
# Prompt format
# --------------------------------------------------------------------------- #


def test_owner_user_prompt_renders_employees_table():
    prompt = build_owner_user_prompt(
        source_text="Иван, сделай X",
        context_messages=[],
        author_user_id="U-author",
        known_employees=[
            {"slack_user_id": "UIVAN", "display_name": "Иван", "real_name": "Ivan Petrov"},
            {"slack_user_id": "UPASHA", "display_name": "Паша", "real_name": "Pavel Sidorov"},
        ],
    )
    assert "known_employees" in prompt
    assert "UIVAN" in prompt
    assert "Иван" in prompt
    assert "UPASHA" in prompt
    assert "Паша" in prompt


def test_owner_user_prompt_omits_table_when_no_employees():
    prompt = build_owner_user_prompt(
        source_text="x",
        context_messages=[],
        author_user_id="U",
        known_employees=[],
    )
    assert "known_employees" not in prompt


# --------------------------------------------------------------------------- #
# Pipeline behaviour with known_employees
# --------------------------------------------------------------------------- #


_TODAY = date(2026, 5, 1)


def test_pipeline_uses_id_from_employees_table():
    """LLM picks the matching slack_user_id from the table for "Иван"."""
    backend = _Backend(
        detect={"is_task": True, "confidence": 0.9},
        title={"title": "сделай отчёт"},
        owner={"slack_user_id": "UIVAN", "display_name": "Иван", "reasoning": "match"},
    )
    out = run_pipeline(
        backend=backend,
        source_text="Иван, сделай отчёт",
        context_messages=[],
        author_user_id="U-author",
        today=_TODAY,
        known_employees=[
            {"slack_user_id": "UIVAN", "display_name": "Иван", "real_name": "Ivan"},
        ],
    )
    assert out.task.owner_user_id == "UIVAN"
    assert out.task.owner_display_name == "Иван"


def test_pipeline_drops_id_that_is_not_in_employees_table():
    """If the LLM hallucinates an id outside the table, drop it."""
    backend = _Backend(
        detect={"is_task": True, "confidence": 0.9},
        title={"title": "сделай отчёт"},
        owner={"slack_user_id": "UFAKE", "display_name": "Хто-то", "reasoning": "hallucinated"},
    )
    out = run_pipeline(
        backend=backend,
        source_text="кто-нибудь сделай отчёт",
        context_messages=[],
        author_user_id="U-author",
        today=_TODAY,
        known_employees=[
            {"slack_user_id": "UIVAN", "display_name": "Иван", "real_name": "Ivan"},
        ],
    )
    # The hallucinated UFAKE is gone; display_name stays so the
    # downstream layer can ask the user to pick.
    assert out.task.owner_user_id is None
    assert out.task.owner_display_name == "Хто-то"


def test_pipeline_resolves_display_name_against_table_when_id_missing():
    """LLM returned only display_name — Python resolver fills in the
    matching slack_user_id from the table."""
    backend = _Backend(
        detect={"is_task": True, "confidence": 0.9},
        title={"title": "сделай"},
        owner={"display_name": "Иван", "reasoning": "name only"},
    )
    out = run_pipeline(
        backend=backend,
        source_text="Иван, сделай",
        context_messages=[],
        author_user_id="U-author",
        today=_TODAY,
        known_employees=[
            {"slack_user_id": "UIVAN", "display_name": "Иван", "real_name": "Ivan Petrov"},
            {"slack_user_id": "UPASHA", "display_name": "Паша", "real_name": "Pavel"},
        ],
    )
    assert out.task.owner_user_id == "UIVAN"
    assert out.task.owner_display_name == "Иван"


def test_pipeline_keeps_display_name_when_no_table_match():
    """Name not in the table → leave display_name, no slack_user_id."""
    backend = _Backend(
        detect={"is_task": True, "confidence": 0.9},
        title={"title": "сделай"},
        owner={"display_name": "Семен", "reasoning": "name only"},
    )
    out = run_pipeline(
        backend=backend,
        source_text="Семен, сделай",
        context_messages=[],
        author_user_id="U-author",
        today=_TODAY,
        known_employees=[
            {"slack_user_id": "UIVAN", "display_name": "Иван", "real_name": "Ivan"},
        ],
    )
    assert out.task.owner_user_id is None
    assert out.task.owner_display_name == "Семен"


def test_node_owner_user_prompt_includes_table_for_lookup():
    """The owner LLM call's user_prompt must contain the table — that's
    the whole point of the fix."""
    backend = _Backend(
        detect={"is_task": True, "confidence": 0.9},
        title={"title": "сделай"},
        owner={"display_name": None, "reasoning": "none"},
    )
    run_pipeline(
        backend=backend,
        source_text="надо что-то",
        context_messages=[],
        author_user_id="U-author",
        today=_TODAY,
        known_employees=[
            {"slack_user_id": "UIVAN", "display_name": "Иван", "real_name": "Ivan"},
        ],
    )
    owner_call = next(c for c in backend.calls if c["tool_name"] == OWNER_TOOL_NAME)
    assert "UIVAN" in owner_call["user_prompt"]
    assert "Иван" in owner_call["user_prompt"]
