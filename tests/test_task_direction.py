"""FR-CR-05-163 — tests for direction classifier + To-Do badges."""
from __future__ import annotations

import json
import types
from datetime import date, time
from typing import Any

import pytest

from app.services.task_direction import (
    DIRECTION_BADGES,
    DIRECTIONS_IMPORTANT,
    classify_directions,
)


class _MockLLM:
    """LLM backend stub returning canned JSON."""

    def __init__(self, response: str) -> None:
        self._response = response
        self.last_user_prompt: str | None = None

    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
    ) -> str:
        self.last_user_prompt = user_prompt
        return self._response


# --- classify_directions ---------------------------------------------


def test_classify_directions_empty_input_returns_empty():
    result = classify_directions(
        tasks=[],
        meeting_context=None,
        llm_backend=_MockLLM('{"items": []}'),
        model="x",
    )
    assert result == {}


def test_classify_directions_happy_path_investors():
    llm = _MockLLM(json.dumps({
        "items": [
            {"task_id": 1, "direction": "investors", "reasoning": "follow-up для investor"},
            {"task_id": 2, "direction": "other", "reasoning": "рутина"},
        ]
    }))
    result = classify_directions(
        tasks=[
            {"id": 1, "title": "Отправить follow-up Mark по раунду", "description": ""},
            {"id": 2, "title": "Закинуть стикеры", "description": ""},
        ],
        meeting_context=None,
        llm_backend=llm,
        model="gpt-5.4",
    )
    assert result == {1: "investors", 2: "other"}


def test_classify_directions_unknown_direction_coerces_to_other():
    llm = _MockLLM(json.dumps({
        "items": [{"task_id": 1, "direction": "nonsense_kind"}],
    }))
    result = classify_directions(
        tasks=[{"id": 1, "title": "X", "description": ""}],
        meeting_context=None, llm_backend=llm, model="x",
    )
    assert result == {1: "other"}


def test_classify_directions_handles_code_fence_wrapped_response():
    raw = "```json\n" + json.dumps({
        "items": [{"task_id": 1, "direction": "budget"}],
    }) + "\n```"
    llm = _MockLLM(raw)
    result = classify_directions(
        tasks=[{"id": 1, "title": "Подготовить P&L", "description": ""}],
        meeting_context=None, llm_backend=llm, model="x",
    )
    assert result == {1: "budget"}


def test_classify_directions_skips_non_int_ids():
    llm = _MockLLM(json.dumps({
        "items": [
            {"task_id": "abc", "direction": "design"},
            {"task_id": 7, "direction": "design"},
        ],
    }))
    result = classify_directions(
        tasks=[{"id": 7, "title": "Дизайн карточек", "description": ""}],
        meeting_context=None, llm_backend=llm, model="x",
    )
    assert result == {7: "design"}


def test_classify_directions_llm_error_returns_empty():
    class _Boom:
        def complete_text(self, **kw):  # noqa: ANN001
            raise RuntimeError("LLM timeout")

    result = classify_directions(
        tasks=[{"id": 1, "title": "X", "description": ""}],
        meeting_context=None, llm_backend=_Boom(), model="x",
    )
    assert result == {}


def test_classify_directions_parse_error_returns_empty():
    llm = _MockLLM("not json at all")
    result = classify_directions(
        tasks=[{"id": 1, "title": "X", "description": ""}],
        meeting_context=None, llm_backend=llm, model="x",
    )
    assert result == {}


def test_classify_directions_passes_context_to_prompt():
    llm = _MockLLM('{"items": []}')
    classify_directions(
        tasks=[{"id": 1, "title": "X", "description": ""}],
        meeting_context="Это очень важный investor call",
        llm_backend=llm, model="x",
    )
    assert "КОНТЕКСТ ВСТРЕЧИ" in (llm.last_user_prompt or "")
    assert "investor call" in (llm.last_user_prompt or "")


# --- DIRECTIONS_IMPORTANT contract -----------------------------------


def test_important_directions_have_badges():
    for d in DIRECTIONS_IMPORTANT:
        assert d in DIRECTION_BADGES, f"missing badge for {d}"
        assert DIRECTION_BADGES[d].strip(), f"empty badge for {d}"


def test_important_directions_set_is_frozen():
    assert "beta" in DIRECTIONS_IMPORTANT
    assert "investors" in DIRECTIONS_IMPORTANT
    assert "other" not in DIRECTIONS_IMPORTANT  # "other" is fallback, не important


# --- _build_todo_section integration (Fireflies) ---------------------


class _StubTask:
    """Minimal Task stub for testing _build_todo_section formatting."""

    def __init__(
        self,
        *,
        id_: int,
        title: str,
        description: str = "",
        owner: str = "",
        direction: str | None = None,
        due_date_: date | None = None,
        due_time_: time | None = None,
    ) -> None:
        self.id = id_
        self.title = title
        self.description = description
        self.owner_display_name = owner
        self.extra = {"direction": direction} if direction else {}
        self.due_date = due_date_
        self.due_time = due_time_
        # Required ORM-like attributes:
        from app.models import TaskSourceKind
        self.source_kind = TaskSourceKind.zoom
        self.source_conversation_id = "test-zoom-id"
        self.deleted_at = None


def _stub_session_with_tasks(tasks_list):
    """Stub session that returns the given tasks from a query chain."""
    class _Query:
        def __init__(self, items):
            self.items = items
        def filter(self, *args, **kwargs):
            return self
        def order_by(self, *args, **kwargs):
            return self
        def all(self):
            return self.items

    class _Sess:
        def query(self, *args, **kwargs):
            return _Query(tasks_list)

    return _Sess()


def test_todo_section_badge_for_important_direction():
    from app.fireflies.pipeline import _build_todo_section
    from app.models import TaskSourceKind

    sess = _stub_session_with_tasks([
        _StubTask(
            id_=1, title="Подготовить pitch deck",
            description="для серии A инвесторов",
            owner="Артем Соколов",
            direction="investors",
            due_date_=date(2026, 5, 12), due_time_=time(15, 0),
        ),
    ])
    rendered = _build_todo_section(
        sess,  # type: ignore[arg-type]
        source_kind=TaskSourceKind.zoom,
        source_conversation_id="test-zoom-id",
    )
    assert "📌 [БЕТА]" not in rendered  # это не beta
    assert "💼 [ИНВЕСТОРЫ]" in rendered
    assert "Артем Соколов" in rendered
    assert "12.05.2026 15:00" in rendered


def test_todo_section_no_badge_for_other_direction():
    from app.fireflies.pipeline import _build_todo_section
    from app.models import TaskSourceKind

    sess = _stub_session_with_tasks([
        _StubTask(
            id_=2, title="Заказать пиццу", owner="Алина",
            direction="other",
            due_date_=date(2026, 5, 12), due_time_=time(18, 0),
        ),
    ])
    rendered = _build_todo_section(
        sess,  # type: ignore[arg-type]
        source_kind=TaskSourceKind.zoom,
        source_conversation_id="test-zoom-id",
    )
    for badge in DIRECTION_BADGES.values():
        assert badge not in rendered, f"unexpected badge {badge}"
    assert "Алина" in rendered
    assert "12.05.2026 18:00" in rendered


def test_todo_section_default_deadline_today_18_00():
    from app.fireflies.pipeline import _build_todo_section
    from app.models import TaskSourceKind

    sess = _stub_session_with_tasks([
        _StubTask(
            id_=3, title="Без явного дедлайна", owner="Дима",
            direction="other",
            due_date_=None, due_time_=None,
        ),
    ])
    rendered = _build_todo_section(
        sess,  # type: ignore[arg-type]
        source_kind=TaskSourceKind.zoom,
        source_conversation_id="test-zoom-id",
    )
    today_str = date.today().strftime("%d.%m.%Y")
    assert today_str in rendered
    assert "18:00" in rendered


def test_todo_section_handles_missing_direction_key():
    """Task без direction вообще (старые row до фикса) — без badge."""
    from app.fireflies.pipeline import _build_todo_section
    from app.models import TaskSourceKind

    sess = _stub_session_with_tasks([
        _StubTask(
            id_=4, title="Стариная задача без direction", owner="Артем",
            direction=None,
            due_date_=date(2026, 5, 11), due_time_=time(12, 0),
        ),
    ])
    rendered = _build_todo_section(
        sess,  # type: ignore[arg-type]
        source_kind=TaskSourceKind.zoom,
        source_conversation_id="test-zoom-id",
    )
    for badge in DIRECTION_BADGES.values():
        assert badge not in rendered
    assert "11.05.2026 12:00" in rendered
