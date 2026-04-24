"""Regression for the intent prompt rules (dates resolved, author ≠ owner)."""
from __future__ import annotations

from datetime import date

from app.intent.prompts import SYSTEM_PROMPT, build_user_prompt


def test_system_prompt_resolves_relative_dates():
    """The system prompt must instruct the LLM to RESOLVE relative /
    weekday phrases against current_date."""
    assert "DO resolve relative" in SYSTEM_PROMPT
    # And give concrete examples the model can pattern-match.
    for example in ("завтра", "к пятнице", "в четверг", "на следующей неделе"):
        assert example in SYSTEM_PROMPT


def test_system_prompt_never_back_dates():
    """Required safeguard: resolved date must be ≥ current_date."""
    assert "Never back-date" in SYSTEM_PROMPT


def test_system_prompt_forbids_author_as_owner():
    """Regression: LLM used to copy the source author's id into
    owner_user_id. The prompt must now forbid that explicitly."""
    assert "NEVER assume the author of the message is the task owner" in SYSTEM_PROMPT
    # And clarifies how to actually fill owner fields.
    assert "<@UXXXX>" in SYSTEM_PROMPT


def test_system_prompt_still_excludes_vague_phrases():
    """Vague phrasing like 'когда-нибудь' must still not create a date."""
    assert "когда-нибудь" in SYSTEM_PROMPT


def test_user_prompt_embeds_weekday_next_to_current_date():
    """Helps the LLM resolve 'пятница' against the right day without an
    external tool."""
    out = build_user_prompt(
        source_text="надо к пятнице",
        context_messages=[],
        invocation_type="passive",
        current_date="2026-04-23",  # Thursday
    )
    # First line includes the date + weekday name.
    first_line = out.splitlines()[0]
    assert "2026-04-23" in first_line
    assert "Thursday" in first_line


def test_user_prompt_annotates_context_user_as_attribution():
    """The 'user' tokens in context rows were confusing the LLM into
    assigning tasks to that user. Prompt now clarifies the semantics."""
    out = build_user_prompt(
        source_text="x",
        context_messages=[{"ts": "1.0", "user": "U-author", "text": "hi"}],
        invocation_type="passive",
        current_date="2026-04-23",
    )
    assert "NOT an assignee" in out
