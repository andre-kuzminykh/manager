"""FR-CR-05-193a — ID-locked tests for Step 1 (single reasoning LLM
extract: summary + tasks в одном JSON).

Operator-pinned 2026-05-22:
  «давай одним размышляющим промтом будем вычленять из транскрипта
   саммери и список всех задач»

Contract:
  - One LLM call (gpt-5.5, reasoning_effort=high).
  - Input: transcript_text + meeting meta (date, duration, host).
  - Output JSON: {summary_detailed, summary_short, tasks:[{raw_owner_mention,
    title, description, due_date?, priority}]}.
  - LLM does NOT resolve owner_mention against TeamMember — raw text only.
  - Empty / corrupt transcript → safe fallback (empty summary + tasks=[]).

Implementation lives in `app/services/reasoning_extract.py::extract_summary_and_tasks`.
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import MagicMock


@pytest.fixture
def mock_llm():
    """Mock OpenAIBackend.chat() — returns whatever .chat.return_value is set."""
    m = MagicMock()
    m.model = "gpt-5.5"
    return m


def test_fr_cr_05_193a_returns_summary_and_tasks_json(mock_llm) -> None:
    """Happy path: LLM outputs valid JSON, function parses and returns dict."""
    from app.services.reasoning_extract import extract_summary_and_tasks

    mock_llm.chat.return_value = json.dumps({
        "summary_detailed": "Обсудили Schaeffler контракт. Дима возьмёт следующий call.",
        "summary_short": "Schaeffler контракт. Дима — call.",
        "tasks": [
            {"raw_owner_mention": "Дима",
             "title": "Взять следующий call по Schaeffler",
             "description": "Подготовить материалы",
             "due_date": "2026-05-23",
             "priority": "high"},
        ],
    })
    result = extract_summary_and_tasks(
        transcript="...long transcript...",
        meeting_date="2026-05-22",
        duration_seconds=1800,
        llm_backend=mock_llm,
        model="gpt-5.5",
    )
    assert "Schaeffler" in result["summary_detailed"]
    assert len(result["tasks"]) == 1
    assert result["tasks"][0]["raw_owner_mention"] == "Дима"
    assert result["tasks"][0]["priority"] == "high"


def test_fr_cr_05_193a_no_resolution_in_step_one(mock_llm) -> None:
    """Step 1 keeps raw mentions. Resolution lives in Step 2 (matcher).
    No TeamMember / Counterparty lookup happens here."""
    from app.services.reasoning_extract import extract_summary_and_tasks
    import inspect

    sig = inspect.signature(extract_summary_and_tasks)
    # MUST NOT accept known_people / known_orgs (those go to Step 2)
    forbidden = {"known_people", "known_orgs", "team_members", "counterparties"}
    assert not (set(sig.parameters.keys()) & forbidden), (
        f"Step 1 must not know about people/orgs DB; got params: "
        f"{set(sig.parameters.keys())}"
    )


def test_fr_cr_05_193a_handles_malformed_llm_json(mock_llm) -> None:
    """LLM returned non-JSON / partial JSON → return empty structure,
    log warning, do not raise."""
    from app.services.reasoning_extract import extract_summary_and_tasks
    mock_llm.chat.return_value = "Sorry I cannot extract from this text"
    result = extract_summary_and_tasks(
        transcript="abc", meeting_date="2026-05-22",
        duration_seconds=300, llm_backend=mock_llm, model="gpt-5.5",
    )
    assert result == {"summary_detailed": "", "summary_short": "", "tasks": []}


def test_fr_cr_05_193a_handles_empty_transcript(mock_llm) -> None:
    """transcript_text='' / None — skip LLM call, return empty structure."""
    from app.services.reasoning_extract import extract_summary_and_tasks
    for empty in ("", None, "   "):
        result = extract_summary_and_tasks(
            transcript=empty, meeting_date="2026-05-22",
            duration_seconds=300, llm_backend=mock_llm, model="gpt-5.5",
        )
        assert result["tasks"] == []
        assert result["summary_detailed"] == ""
    # LLM never called for empty input
    assert mock_llm.chat.call_count == 0


def test_fr_cr_05_193a_each_task_has_required_fields(mock_llm) -> None:
    """Each task in output MUST have raw_owner_mention + title (others optional).
    Tasks missing required field are dropped from result."""
    from app.services.reasoning_extract import extract_summary_and_tasks
    mock_llm.chat.return_value = json.dumps({
        "summary_detailed": "ok",
        "summary_short": "ok",
        "tasks": [
            {"raw_owner_mention": "Дима", "title": "Valid task"},
            {"title": "Task without owner — must be dropped"},  # missing raw_owner
            {"raw_owner_mention": "Игорь"},                    # missing title
            {"raw_owner_mention": "Иван", "title": "Valid 2"},
        ],
    })
    result = extract_summary_and_tasks(
        transcript="x", meeting_date="2026-05-22",
        duration_seconds=300, llm_backend=mock_llm, model="gpt-5.5",
    )
    titles = [t["title"] for t in result["tasks"]]
    assert "Valid task" in titles
    assert "Valid 2" in titles
    assert len(result["tasks"]) == 2  # dropped 2 invalid


def test_fr_cr_05_193a_due_date_priority_optional(mock_llm) -> None:
    """due_date + priority НЕ обязательны. Default = None / "medium"."""
    from app.services.reasoning_extract import extract_summary_and_tasks
    mock_llm.chat.return_value = json.dumps({
        "summary_detailed": "x", "summary_short": "x",
        "tasks": [{"raw_owner_mention": "Дима", "title": "T1"}],
    })
    result = extract_summary_and_tasks(
        transcript="x", meeting_date="2026-05-22",
        duration_seconds=300, llm_backend=mock_llm, model="gpt-5.5",
    )
    t = result["tasks"][0]
    assert t.get("due_date") is None or t.get("due_date") == ""
    assert t.get("priority", "medium") in ("low", "medium", "high", "urgent")


def test_fr_cr_05_193a_summary_short_is_subset_or_distinct(mock_llm) -> None:
    """summary_short — самостоятельное короткое описание, не префикс detailed.
    Contract — обе строки заполнены, both non-empty."""
    from app.services.reasoning_extract import extract_summary_and_tasks
    mock_llm.chat.return_value = json.dumps({
        "summary_detailed": "Long detailed summary " * 50,
        "summary_short": "Brief short summary.",
        "tasks": [],
    })
    result = extract_summary_and_tasks(
        transcript="x", meeting_date="2026-05-22",
        duration_seconds=300, llm_backend=mock_llm, model="gpt-5.5",
    )
    assert len(result["summary_short"]) < len(result["summary_detailed"])
    assert result["summary_short"]  # not empty
    assert result["summary_detailed"]  # not empty


def test_fr_cr_05_193a_single_llm_call_per_invocation(mock_llm) -> None:
    """ОДИН reasoning call per extract — no retry / multi-step internal logic
    (matcher идёт отдельным вызовом снаружи). FR-CR-05-193 contract."""
    from app.services.reasoning_extract import extract_summary_and_tasks
    mock_llm.chat.return_value = json.dumps({
        "summary_detailed": "x", "summary_short": "x", "tasks": [],
    })
    extract_summary_and_tasks(
        transcript="something long enough", meeting_date="2026-05-22",
        duration_seconds=300, llm_backend=mock_llm, model="gpt-5.5",
    )
    assert mock_llm.chat.call_count == 1


def test_fr_cr_05_193a_5_prompt_extracts_implicit_internal_followups() -> None:
    """FR-CR-05-193a-5 — Step 1 prompt должен инструктировать LLM
    выделять НЕЯВНЫЕ follow-ups для нашей стороны (обновить data room,
    отправить deck, передать контакты и т.д.), а не только явные
    «я сделаю X» утверждения. Это критично для investor / vendor /
    candidate meetings где большинство follow-up действий не
    проговариваются явно как commitment.
    """
    from app.services.reasoning_extract import _SYSTEM_PROMPT
    # Канонический язык
    assert "НЕЯВНЫЕ" in _SYSTEM_PROMPT or "implicit" in _SYSTEM_PROMPT.lower()
    # Примеры стандартных post-meeting follow-up действий
    assert "data room" in _SYSTEM_PROMPT
    assert "deck" in _SYSTEM_PROMPT.lower()
    assert "intro" in _SYSTEM_PROMPT.lower() or "follow-up" in _SYSTEM_PROMPT.lower()
    # Указание что 2-3 tasks для 30-min meeting = пропуски
    assert "пропустил" in _SYSTEM_PROMPT or "Excessive" in _SYSTEM_PROMPT or "missed" in _SYSTEM_PROMPT.lower()
    # Указание использовать «мы» когда нет явного name
    assert "мы" in _SYSTEM_PROMPT
