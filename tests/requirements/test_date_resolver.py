"""Requirement coverage: FR-CR-04-3 (deterministic date resolver),
FR-CR-04-5 (strip_date_phrase).

gpt-4o-mini ignores the weekday lookup table; this safety net catches
the misses deterministically from the source text."""
from __future__ import annotations

from datetime import date

import pytest

from app.intent.date_resolver import resolve_due_date, strip_date_phrase

# 2026-04-24 is a Friday.
FRIDAY = date(2026, 4, 24)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("надо подготовить питчдек к понедельнику", date(2026, 4, 27)),
        ("сделай до вторника", date(2026, 4, 28)),
        ("пришлёшь в среду?", date(2026, 4, 29)),
        ("сделай к четвергу", date(2026, 4, 30)),
        ("подготовь до пятницы", date(2026, 5, 1)),  # today is Friday → NEXT
        ("сдать в субботу", date(2026, 4, 25)),
        ("сделать в воскресенье", date(2026, 4, 26)),
        ("до завтра", date(2026, 4, 25)),
        ("послезавтра", date(2026, 4, 26)),
        ("сегодня", date(2026, 4, 24)),
        ("к концу недели", date(2026, 5, 1)),
        ("на следующей неделе", date(2026, 4, 27)),
        ("прислать к Monday", date(2026, 4, 27)),
        ("do it by Friday", date(2026, 5, 1)),
        ("see tomorrow", date(2026, 4, 25)),
        ("deadline 2026-05-12", date(2026, 5, 12)),
        # Russian day + month-name (genitive).
        ("надо подготовить заметки к 1 мая", date(2026, 5, 1)),
        ("до 5 июня отчёт", date(2026, 6, 5)),
        ("к 25 декабря", date(2026, 12, 25)),
        ("до 30 апреля", date(2026, 4, 30)),
        # English "<month> <day>".
        ("by May 5", date(2026, 5, 5)),
        ("May 1st deadline", date(2026, 5, 1)),
        ("by Jun 15th", date(2026, 6, 15)),
        # Day + month that already passed this year → next year.
        ("к 1 января", date(2027, 1, 1)),
        # "через N <unit>" and English "in N <unit>".
        ("мне надо статью написать через неделю", date(2026, 5, 1)),
        ("через 2 недели", date(2026, 5, 8)),
        ("через 3 дня", date(2026, 4, 27)),
        ("через день", date(2026, 4, 25)),
        ("через месяц", date(2026, 5, 24)),
        ("in a week we ship", date(2026, 5, 1)),
        ("in 2 days", date(2026, 4, 26)),
        ("in 3 weeks", date(2026, 5, 15)),
    ],
)
def test_resolve_due_date_hits(text, expected):
    assert resolve_due_date(text, FRIDAY) == expected


def test_resolve_due_date_returns_none_when_vague():
    assert resolve_due_date("когда-нибудь сделаем", FRIDAY) is None
    assert resolve_due_date("", FRIDAY) is None
    assert resolve_due_date("надо собрать демо", FRIDAY) is None


def test_classifier_fills_missing_due_date_locally(monkeypatch):
    """When the LLM returns a task with due_date=None but the source text
    names a weekday, classify_with_backend should fill it in."""
    from app.context.retriever import ContextWindow
    from app.intent.classifier import classify_with_backend
    from app.schemas.intent import InvocationType

    class _Backend:
        def extract_intent(self, *, user_prompt):
            return {
                "intent": "create_task",
                "confidence": 0.9,
                "task": {"title": "питчдек", "due_date": None},
            }

        def call_tool(self, **kw):
            # Owner follow-up call — test does not care about owner here.
            return {"reasoning": "no assignee", "display_name": None}

    # Freeze today to Friday 2026-04-24.
    import app.intent.classifier as clf

    class _FrozenDate(date):
        @classmethod
        def today(cls):  # type: ignore[override]
            return FRIDAY

    monkeypatch.setattr(clf, "date", _FrozenDate)

    ctx = ContextWindow(
        conversation_id="C1",
        source_ts="1.0",
        thread_ts=None,
        source_message={"ts": "1.0", "text": "надо подготовить питчдек к понедельнику"},
    )
    result = classify_with_backend(
        backend=_Backend(),
        context=ctx,
        invocation_type=InvocationType.mention,
        source_text="надо подготовить питчдек к понедельнику",
    )
    assert result.task is not None
    assert result.task.due_date == date(2026, 4, 27)


@pytest.mark.parametrize(
    "title,expected",
    [
        ("подготовить заметки к 1 мая", "подготовить заметки"),
        ("собрать демо ко вторнику", "собрать демо"),
        ("отчёт до пятницы", "отчёт"),
        ("call Ivan by Friday", "call Ivan"),
        ("подготовить питчдек", "подготовить питчдек"),
        ("сделать 2026-05-12", "сделать"),
        ("сделать завтра", "сделать"),
        ("prepare deck for May 1st", "prepare deck for"),
        ("написать статью через неделю", "написать статью"),
        ("отгрузить через 3 дня", "отгрузить"),
        ("ship this in 3 days", "ship this"),
        ("publish in a week", "publish"),
    ],
)
def test_strip_date_phrase(title, expected):
    assert strip_date_phrase(title) == expected
