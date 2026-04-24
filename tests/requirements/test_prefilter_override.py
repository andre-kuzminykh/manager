"""Requirement coverage: FR-CR-04-9 (prefilter safety net).

When the LLM returns no_action but the rule-based prefilter strongly
matches a task/meeting keyword, classify_with_backend overrides the
LLM's answer with the prefilter's hint. This keeps the passive path from
going silent on clear phrases like "надо подготовить заметки к 1 мая"
that gpt-4o-mini occasionally misclassifies."""
from __future__ import annotations

from app.context.retriever import ContextWindow
from app.intent.classifier import classify_with_backend
from app.schemas.intent import InvocationType, IntentType


class _Backend:
    """Pipeline-aware stub: dispatches a different payload per tool.

    `detect` controls whether Stage 1 says the message is a task at all.
    `title` / `owner` are the Stage-2 responses (unused if Stage 1 is
    negative).
    """

    def __init__(self, *, detect=None, title=None, owner=None):
        from app.intent.detect_prompt import DETECT_TOOL_NAME
        from app.intent.owner_prompt import OWNER_TOOL_NAME
        from app.intent.title_prompt import TITLE_TOOL_NAME

        self._payloads = {
            DETECT_TOOL_NAME: detect,
            TITLE_TOOL_NAME: title,
            OWNER_TOOL_NAME: owner,
        }

    def extract_intent(self, *, user_prompt):  # pragma: no cover
        raise NotImplementedError

    def call_tool(self, **kw):
        return self._payloads.get(kw.get("tool_name"))


def _ctx(text):
    return ContextWindow(
        conversation_id="C1",
        source_ts="1.0",
        thread_ts=None,
        source_message={"ts": "1.0", "user": "U-author", "text": text},
    )


def test_prefilter_overrides_no_action_for_task_keywords():
    backend = _Backend(detect={"is_task": False, "confidence": 0.1})
    out = classify_with_backend(
        backend=backend,
        context=_ctx("надо подготовить заметки к 1 мая"),
        invocation_type=InvocationType.passive,
        source_text="надо подготовить заметки к 1 мая",
    )
    assert out.intent == IntentType.create_task
    assert out.task is not None
    assert out.task.title.startswith("надо подготовить заметки")
    # Date resolver still fires on top of the synthesised draft.
    assert out.task.due_date is not None
    assert out.task.due_date.month == 5
    assert out.task.due_date.day == 1


def test_prefilter_overrides_no_action_for_meeting_keywords():
    backend = _Backend(detect={"is_task": False, "confidence": 0.1})
    out = classify_with_backend(
        backend=backend,
        context=_ctx("давайте созвон завтра в 11"),
        invocation_type=InvocationType.passive,
        source_text="давайте созвон завтра в 11",
    )
    assert out.intent == IntentType.create_meeting
    assert out.meeting is not None


def test_prefilter_does_not_override_when_no_keyword():
    backend = _Backend(detect={"is_task": False, "confidence": 0.1})
    out = classify_with_backend(
        backend=backend,
        context=_ctx("спасибо за кофе!"),
        invocation_type=InvocationType.passive,
        source_text="спасибо за кофе!",
    )
    assert out.intent == IntentType.no_action
    assert out.task is None


def test_pipeline_create_task_wins_over_prefilter():
    """If the pipeline detected a task, we don't run the prefilter
    override — the LLM's answer stands."""
    backend = _Backend(
        detect={"is_task": True, "confidence": 0.92, "reasoning": "yes"},
        title={"title": "prepared LLM title"},
        owner={"reasoning": "no assignee", "display_name": None},
    )
    out = classify_with_backend(
        backend=backend,
        context=_ctx("надо сделать X"),
        invocation_type=InvocationType.passive,
        source_text="надо сделать X",
    )
    assert out.intent == IntentType.create_task
    assert out.task.title == "prepared LLM title"
    assert out.confidence == 0.92
