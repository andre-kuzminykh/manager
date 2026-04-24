"""Requirement coverage: FR-CR-04-1 (Detection stage),
FR-CR-04-2 (Parallel extraction), FR-CR-04-5 (date stripping from
title/description), NFR-CR-04-1 (per-stage resilience).

Pipeline architecture tests:
  Stage 1 — a single LLM call decides: is this a task? (yes / no).
  Stage 2 — if yes, three concerns are extracted IN PARALLEL:
            2a. description / title / priority  (LLM)
            2b. owner                           (LLM, conversation-aware)
            2c. due date                        (Python resolver)
  Stage 3 — assemble and return IntentClassification.
  UX: mention → auto-confirm; passive → needs confirmation.
"""
from __future__ import annotations

import threading
import time
from datetime import date

from app.intent.detect_prompt import DETECT_SYSTEM_PROMPT, DETECT_TOOL_NAME
from app.intent.owner_prompt import OWNER_SYSTEM_PROMPT, OWNER_TOOL_NAME
from app.intent.pipeline import run_pipeline
from app.intent.title_prompt import TITLE_SYSTEM_PROMPT, TITLE_TOOL_NAME
from app.schemas.intent import IntentType


# -------- Stage prompt sanity --------------------------------------------- #


def test_detect_prompt_asks_single_yes_no_question():
    # The detection prompt knows nothing about extraction. Its schema has
    # exactly is_task + confidence + reasoning.
    from app.intent.detect_prompt import DETECT_TOOL_PARAMETERS

    props = DETECT_TOOL_PARAMETERS["properties"]
    assert set(props.keys()) == {"is_task", "confidence", "reasoning"}
    assert DETECT_TOOL_PARAMETERS["required"] == ["is_task", "confidence"]
    assert "is_task" in DETECT_SYSTEM_PROMPT.lower() or "is the author" in DETECT_SYSTEM_PROMPT.lower()


def test_title_prompt_covers_title_description_priority_only():
    from app.intent.title_prompt import TITLE_TOOL_PARAMETERS

    props = TITLE_TOOL_PARAMETERS["properties"]
    assert set(props.keys()) == {"title", "description", "priority"}
    # The tool schema must NOT expose assignee or due_date fields.
    assert "due_date" not in props
    assert "owner" not in props
    assert "assignee" not in props


def test_owner_prompt_sees_conversation_context_placeholder():
    from app.intent.owner_prompt import build_owner_user_prompt

    prompt = build_owner_user_prompt(
        source_text="сделай это",
        context_messages=[{"ts": "0.5", "user": "U-a", "text": "hello"}],
        author_user_id="U-a",
    )
    assert "context" in prompt.lower()
    assert "source_author: U-a" in prompt


# -------- Recording backend ------------------------------------------------ #


class _RecordingBackend:
    def __init__(self, *, detect, title, owner, delay_title: float = 0):
        self._payloads = {
            DETECT_TOOL_NAME: detect,
            TITLE_TOOL_NAME: title,
            OWNER_TOOL_NAME: owner,
        }
        self._delay_title = delay_title
        self.call_log: list[tuple[str, float]] = []
        self._lock = threading.Lock()

    def extract_intent(self, *, user_prompt):  # pragma: no cover
        raise NotImplementedError

    def call_tool(self, **kw):
        tool = kw["tool_name"]
        start = time.perf_counter()
        if tool == TITLE_TOOL_NAME and self._delay_title:
            time.sleep(self._delay_title)
        end = time.perf_counter()
        with self._lock:
            self.call_log.append((tool, start))
            self.call_log.append((tool + ":end", end))
        return self._payloads.get(tool)


# -------- Stage 1 short-circuit ------------------------------------------- #


def test_detection_false_short_circuits_the_pipeline():
    backend = _RecordingBackend(
        detect={"is_task": False, "confidence": 0.05, "reasoning": "chat"},
        title={"title": "unused"},
        owner={"reasoning": "unused", "display_name": None},
    )
    out = run_pipeline(
        backend=backend,
        source_text="how's it going?",
        context_messages=[],
        author_user_id="U-author",
        today=date(2026, 4, 24),
    )
    assert out.intent == IntentType.no_action
    # Stage 2 never ran.
    tools_called = {
        entry[0] for entry in backend.call_log if not entry[0].endswith(":end")
    }
    assert tools_called == {DETECT_TOOL_NAME}


# -------- Stage 2 parallelism --------------------------------------------- #


def test_stage2_title_and_owner_run_in_parallel():
    """Title stage sleeps 80ms. Owner stage is instant. If we ran them
    sequentially, total Stage-2 time would be ~80ms; running in parallel
    caps it near 80ms regardless of the owner stage. We verify by
    checking that the two start timestamps are nearly simultaneous —
    title should start BEFORE owner finished, i.e. they overlap."""
    backend = _RecordingBackend(
        detect={"is_task": True, "confidence": 0.9},
        title={"title": "собрать демо"},
        owner={"reasoning": "no one", "display_name": None},
        delay_title=0.08,
    )
    run_pipeline(
        backend=backend,
        source_text="надо собрать демо",
        context_messages=[],
        author_user_id="U-author",
        today=date(2026, 4, 24),
    )
    starts = {
        entry[0]: entry[1]
        for entry in backend.call_log
        if not entry[0].endswith(":end")
    }
    ends = {
        entry[0][:-4]: entry[1]
        for entry in backend.call_log
        if entry[0].endswith(":end")
    }
    # Owner started before title finished → they overlap in time.
    assert starts[OWNER_TOOL_NAME] < ends[TITLE_TOOL_NAME], (
        "Stage 2 must run title and owner concurrently, not sequentially"
    )


# -------- Stage 3 assembly ------------------------------------------------- #


def test_pipeline_assembles_all_four_fields():
    backend = _RecordingBackend(
        detect={"is_task": True, "confidence": 0.88, "reasoning": "yes"},
        title={
            "title": "подготовить питчдек",
            "description": "для инвесторов",
            "priority": "high",
        },
        owner={
            "slack_user_id": "U-ivan",
            "display_name": "Иван",
            "reasoning": "addressed to <@U-ivan>",
        },
    )
    out = run_pipeline(
        backend=backend,
        source_text="Иван, подготовь питчдек к понедельнику",
        context_messages=[],
        author_user_id="U-author",
        today=date(2026, 4, 24),
    )
    assert out.intent == IntentType.create_task
    assert out.confidence == 0.88
    assert out.task.title == "подготовить питчдек"
    assert out.task.description == "для инвесторов"
    assert out.task.priority == "high"
    assert out.task.owner_user_id == "U-ivan"
    assert out.task.owner_display_name == "Иван"
    # Stage 2c: Python date resolver picked up "понедельнику".
    assert out.task.due_date == date(2026, 4, 27)


def test_pipeline_strips_date_from_title_and_description():
    backend = _RecordingBackend(
        detect={"is_task": True, "confidence": 0.88},
        title={
            "title": "подготовить заметки к 1 мая",
            "description": "к 1 мая",
            "priority": "high",
        },
        owner={"reasoning": "no assignee", "display_name": None},
    )
    out = run_pipeline(
        backend=backend,
        source_text="надо подготовить заметки к 1 мая",
        context_messages=[],
        author_user_id="U-author",
        today=date(2026, 4, 24),
    )
    assert out.task.title == "подготовить заметки"
    # Description was just "к 1 мая" → becomes None after stripping.
    assert out.task.description is None
    # Date still ends up in due_date.
    assert out.task.due_date == date(2026, 5, 1)


def test_pipeline_leaves_owner_null_when_owner_stage_says_no_one():
    backend = _RecordingBackend(
        detect={"is_task": True, "confidence": 0.9},
        title={"title": "сделай отчёт"},
        owner={"reasoning": "no assignee wording", "display_name": None},
    )
    out = run_pipeline(
        backend=backend,
        source_text="надо сделать отчёт",
        context_messages=[],
        author_user_id="U-author",
        today=date(2026, 4, 24),
    )
    assert out.task is not None
    assert out.task.owner_user_id is None
    assert out.task.owner_display_name is None


def test_pipeline_uses_display_name_when_no_slack_id():
    backend = _RecordingBackend(
        detect={"is_task": True, "confidence": 0.9},
        title={"title": "сделай"},
        owner={"display_name": "Паша", "reasoning": "assigned to Паша"},
    )
    out = run_pipeline(
        backend=backend,
        source_text="Паша, сделай",
        context_messages=[],
        author_user_id="U-author",
        today=date(2026, 4, 24),
    )
    assert out.task.owner_display_name == "Паша"
    assert out.task.owner_user_id is None


# -------- Resilience ------------------------------------------------------- #


def test_title_stage_failure_falls_back_to_source_text():
    class _BrokenTitle(_RecordingBackend):
        def call_tool(self, **kw):
            if kw["tool_name"] == TITLE_TOOL_NAME:
                raise RuntimeError("openai down")
            return super().call_tool(**kw)

    backend = _BrokenTitle(
        detect={"is_task": True, "confidence": 0.9},
        title={"title": "unused"},
        owner={"reasoning": "no one", "display_name": None},
    )
    out = run_pipeline(
        backend=backend,
        source_text="надо сделать X",
        context_messages=[],
        author_user_id="U-author",
        today=date(2026, 4, 24),
    )
    assert out.intent == IntentType.create_task
    assert out.task.title  # falls back to source_text[:120] or similar


def test_detect_stage_failure_returns_no_action():
    class _BrokenDetect:
        def extract_intent(self, *, user_prompt):
            raise NotImplementedError

        def call_tool(self, **kw):
            if kw["tool_name"] == DETECT_TOOL_NAME:
                raise RuntimeError("openai down")
            return None

    out = run_pipeline(
        backend=_BrokenDetect(),
        source_text="надо сделать",
        context_messages=[],
        author_user_id="U-author",
        today=date(2026, 4, 24),
    )
    # The pipeline swallows the failure and emits no_action. The
    # classify_with_backend safety net (separate concern) handles the
    # rules-based override above this layer.
    assert out.intent == IntentType.no_action
