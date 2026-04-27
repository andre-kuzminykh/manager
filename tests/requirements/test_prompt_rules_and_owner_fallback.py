"""Regression for two intertwined product rules:

1. The intent prompt must make the LLM RESOLVE relative date phrases
   (tomorrow, "к пятнице", "на следующей неделе", etc.) against
   current_date — and NOT back-date.
2. If the LLM didn't extract an owner, persistence falls back to the
   source-message author BUT marks the task with extra.owner_assumed so
   the UI can show "(предположительно ты)"."""
from __future__ import annotations

from datetime import date

from app.intent.prompts import SYSTEM_PROMPT, build_user_prompt
from app.models import (
    ActionDraft,
    ActionDraftState,
    ContextSnapshot,
    IntentInference,
    Task,
    TaskStatus,
)
from app.models.intent import IntentType as IE
from app.persistence import create_task_from_draft
from app.slack_bot import blocks as bk


# --------------------------------------------------------------------------- #
# Prompt rules
# --------------------------------------------------------------------------- #


def test_prompt_tells_llm_to_resolve_relative_dates():
    assert "DO resolve relative" in SYSTEM_PROMPT
    for example in ("завтра", "к пятнице", "в четверг", "на следующей неделе"):
        assert example in SYSTEM_PROMPT


def test_prompt_forbids_back_dating():
    assert "Never back-date" in SYSTEM_PROMPT


def test_prompt_keeps_vague_phrases_null():
    assert "когда-нибудь" in SYSTEM_PROMPT


def test_prompt_still_forbids_guessing_owner():
    # Rule 6 forbids assuming the author is the owner, even while allowing
    # the downstream fallback to happen with a "(implicit)" label.
    assert "NEVER assume the author of the message is the task owner" in SYSTEM_PROMPT
    assert "предположительно ты" in SYSTEM_PROMPT  # documents the downstream UI


def test_user_prompt_includes_weekday_label():
    text = build_user_prompt(
        source_text="надо к пятнице",
        context_messages=[],
        invocation_type="passive",
        current_date="2026-04-23",
    )
    first_line = text.splitlines()[0]
    assert "2026-04-23" in first_line
    assert "Thursday" in first_line


def test_user_prompt_emits_weekday_lookup_table():
    """Regression for LLM off-by-days errors: the prompt now carries a
    pre-computed table mapping each weekday to the next-upcoming ISO date."""
    text = build_user_prompt(
        source_text="к понедельнику",
        context_messages=[],
        invocation_type="passive",
        current_date="2026-04-24",  # Friday
    )
    assert "Weekday lookup" in text
    # Next Monday after Friday 2026-04-24 is 2026-04-27.
    assert "Monday     → 2026-04-27" in text
    # And Saturday in the table is 2026-04-25.
    assert "Saturday   → 2026-04-25" in text
    # And the named Friday entry refers to NEXT Friday, not today.
    assert "Friday     → 2026-05-01" in text


def test_user_prompt_weekday_lookup_always_strictly_future():
    """Never map a day name to today — the lookup offsets start at +1 so
    'к пятнице' on a Friday resolves to next week, avoiding the today-vs-
    next-week ambiguity the user hit."""
    text = build_user_prompt(
        source_text="x",
        context_messages=[],
        invocation_type="passive",
        current_date="2026-04-24",  # Friday
    )
    # Table should not contain 2026-04-24 itself — everything is >= +1.
    assert "2026-04-24" not in text.split("Weekday lookup")[1]


def test_user_prompt_labels_context_user_as_attribution():
    text = build_user_prompt(
        source_text="x",
        context_messages=[{"ts": "1.0", "user": "U1", "text": "hi"}],
        invocation_type="passive",
        current_date="2026-04-23",
    )
    assert "NOT an assignee" in text


# --------------------------------------------------------------------------- #
# Owner fallback → extra.owner_assumed
# --------------------------------------------------------------------------- #


def _make_draft(session, payload):
    snap = ContextSnapshot(
        conversation_id="C1",
        source_ts="1.0",
        source_message={"ts": "1.0", "text": "x", "user": "U-author"},
        history_before=[],
        thread_messages=[],
    )
    session.add(snap)
    session.flush()
    inf = IntentInference(
        context_snapshot_id=snap.id,
        intent=IE.create_task,
        confidence=0.9,
        invocation_type="passive",
    )
    session.add(inf)
    session.flush()
    draft = ActionDraft(
        inference_id=inf.id,
        intent=IE.create_task,
        state=ActionDraftState.proposed,
        payload=payload,
        created_by_slack_user_id="U-author",
        slack_message_ts="1.0",
    )
    session.add(draft)
    session.flush()
    return draft


def test_no_owner_mentioned_falls_back_to_author_and_marks_assumed(session):
    draft = _make_draft(session, payload={"title": "prep deck"})
    t = create_task_from_draft(
        session,
        draft=draft,
        source={},
        context_snapshot_id=None,
        fallback_author_slack_id="U-author",
    )
    assert t.owner_user_id == "U-author"
    assert (t.extra or {}).get("owner_assumed") is True


def test_explicit_owner_is_not_marked_assumed(session):
    draft = _make_draft(
        session,
        payload={
            "title": "prep deck",
            "owner_user_id": "U-ivan",
            "owner_display_name": "Иван",
        },
    )
    t = create_task_from_draft(
        session,
        draft=draft,
        source={},
        context_snapshot_id=None,
        fallback_author_slack_id="U-author",
    )
    assert t.owner_user_id == "U-ivan"
    assert not (t.extra or {}).get("owner_assumed")


def test_unresolved_name_does_not_flag_assumed(session):
    """User said 'на Семёна', Семён not in allowed list → display_name
    stays but owner_user_id is empty. Not a fallback-to-author situation."""
    draft = _make_draft(
        session,
        payload={"title": "prep deck", "owner_display_name": "Семён"},
    )
    t = create_task_from_draft(
        session,
        draft=draft,
        source={},
        context_snapshot_id=None,
        fallback_author_slack_id="U-author",
    )
    assert t.owner_user_id is None
    assert not (t.extra or {}).get("owner_assumed")


# --------------------------------------------------------------------------- #
# task_card / admin_review_card render the "(implicit)" hint
# --------------------------------------------------------------------------- #


def _task(session, **kw):
    t = Task(title=kw.pop("title", "t"), status=kw.pop("status", TaskStatus.todo), **kw)
    session.add(t)
    session.flush()
    return t


def test_task_card_shows_assumed_suffix_when_extra_flag_set(session):
    t = _task(session, owner_user_id="U-author", extra={"owner_assumed": True})
    blocks = bk.task_card(task=t, viewer_slack_user_id="U-author")
    meta = "\n".join(
        el["text"]
        for b in blocks
        if b.get("type") == "context"
        for el in b["elements"]
    )
    assert "implicit" in meta


def test_task_card_no_assumed_suffix_for_explicit_owner(session):
    t = _task(session, owner_user_id="U-ivan", extra=None)
    blocks = bk.task_card(task=t, viewer_slack_user_id="U-ivan")
    meta = "\n".join(
        el["text"]
        for b in blocks
        if b.get("type") == "context"
        for el in b["elements"]
    )
    assert "предположительно" not in meta


def test_admin_review_card_shows_assumed_suffix(session):
    t = _task(session, owner_user_id="U-author", extra={"owner_assumed": True})
    blocks = bk.admin_review_card(task=t)
    fields_section = next(b for b in blocks if b.get("type") == "section" and "fields" in b)
    owner_text = next(
        f["text"] for f in fields_section["fields"] if "Owner" in f["text"]
    )
    assert "implicit" in owner_text


def test_admin_review_card_no_assumed_suffix_when_explicit(session):
    t = _task(session, owner_user_id="U-ivan", extra=None)
    blocks = bk.admin_review_card(task=t)
    fields_section = next(b for b in blocks if b.get("type") == "section" and "fields" in b)
    owner_text = next(
        f["text"] for f in fields_section["fields"] if "Owner" in f["text"]
    )
    assert "предположительно" not in owner_text
