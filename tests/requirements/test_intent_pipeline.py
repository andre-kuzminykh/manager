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
    # The detection schema covers the binary verdict + the
    # FR-CR-05-05 multi-task-split signal (task_count + task_chunks).
    from app.intent.detect_prompt import DETECT_TOOL_PARAMETERS

    props = DETECT_TOOL_PARAMETERS["properties"]
    assert set(props.keys()) == {
        "is_task",
        "confidence",
        "reasoning",
        "task_count",
        "task_chunks",
    }
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


def test_detect_prompt_lists_status_reports_and_parroted_phrases_as_no_action():
    """FR-CR-05-09 — the detect prompt is taught to reject:

    - status-list reports («DBS — нет, Jefferies — отправила, …»)
    - parroted one-line acknowledgements without context
      («хорошо! напишу ему», «ок, сделаю»)
    - OCR / transcription noise («файндхэзом» as the entire message)

    These show up enough in real chat traffic that we want them
    pinned in the prompt, not just folded into the generic «chat»
    bucket.
    """
    blob = DETECT_SYSTEM_PROMPT
    assert "Status-list reports" in blob or "status-list" in blob.lower()
    assert "Parroted" in blob or "parroted" in blob.lower()
    # At least one example each side of the dash for status lists.
    assert "DBS" in blob or "Jefferies" in blob
    # Parroted-phrase example must be quoted so the model anchors
    # on the exact pattern.
    assert "напишу ему" in blob
    # OCR-noise rejection must be explicit — we hit «файндхэзом»
    # in real traffic and want it killed at the detect stage.
    assert "OCR" in blob or "transcription" in blob.lower()


def test_detect_prompt_rejects_passive_past_tense_status_reports():
    """FR-CR-05-12 — «письма в Abundance отправлены» (passive past
    tense) must be no_action, same as active past («отправил»).
    The prompt now lists passive forms explicitly so the LLM
    doesn't read them as imperatives."""
    blob = DETECT_SYSTEM_PROMPT
    # Passive forms that previously slipped through.
    for word in ("отправлены", "подписан", "оплачен", "утверждён", "sent", "approved"):
        assert word in blob, (
            f"detect prompt should mention passive-past form {word!r}"
        )
    # Concrete example anchoring the rule.
    assert "письма в Abundance отправлены" in blob


def test_title_prompt_teaches_imperative_rewrite_from_context():
    """FR-CR-05-09 — the title prompt is taught to use the
    `context` block to rewrite parroted one-liners into a proper
    imperative title. «хорошо, напишу ему» with a prior message
    «надо ответить Андрею» must NOT land as the literal phrase —
    the prompt explicitly forbids it and shows a rewrite example.
    """
    blob = TITLE_SYSTEM_PROMPT
    assert "PARROTED" in blob or "parroted" in blob.lower()
    # Forbid the verbatim copy and show the proper rewrite shape.
    assert "напишу ему" in blob
    assert "написать" in blob
    # The instruction MUST mention the context block, since the
    # rewrite depends on it.
    assert "context" in blob.lower()


def test_title_prompt_forbids_third_party_status_promises():
    """FR-CR-05-13 — «Нет Алина сама отправит» (a third-party
    promise sentence about another teammate's commitment) must NOT
    land verbatim as the title. The title prompt teaches an
    explicit rewrite path."""
    blob = TITLE_SYSTEM_PROMPT
    assert "THIRD-PARTY" in blob or "third-party" in blob.lower()
    assert "Нет Алина сама отправит" in blob
    # The example must show a clean imperative as the rewrite.
    assert "отправить" in blob


def test_title_prompt_forbids_trailing_clauses_in_descriptions():
    """FR-CR-05-12 — the title prompt now caps the description at
    1-3 short sentences and explicitly forbids trailing-clause
    truncations like «так как осталось открытым с» (mid-sentence
    cut)."""
    blob = TITLE_SYSTEM_PROMPT
    # Length / completeness rule must be present.
    assert "LENGTH RULE" in blob or "length rule" in blob.lower()
    assert "complete sentence" in blob.lower() or "finish every sentence" in blob.lower()
    assert "trail off" in blob.lower() or "trailing" in blob.lower()


def test_owner_prompt_renders_role_and_notes_columns():
    """FR-CR-05-12 — the owner prompt receives `role` and `notes`
    per known_employee row to disambiguate same-first-name
    teammates («Алина — founder» vs «Алина — project manager»).
    The user-side prompt must surface those columns."""
    from app.intent.owner_prompt import build_owner_user_prompt

    prompt = build_owner_user_prompt(
        source_text="подать заявку на StartUp Qatar",
        context_messages=[],
        author_user_id="U1",
        known_employees=[
            {
                "slack_user_id": "111",
                "display_name": "Alina",
                "real_name": "Alina Founder",
                "role": "founder",
                "notes": "deals with international expansion",
            },
            {
                "slack_user_id": "222",
                "display_name": "Alina",
                "real_name": "Alina Manager",
                "role": "project manager / аналитик",
                "notes": "",
            },
        ],
    )
    # Both role strings must appear so the LLM can tell them apart.
    assert "founder" in prompt
    assert "project manager" in prompt.lower()
    # Header advertises the new columns.
    assert "role" in prompt
    assert "notes" in prompt


def test_owner_prompt_disambiguation_section_lists_role_first():
    """The system prompt must teach the LLM to USE role / notes
    when several rows share a first name. If we don't pin this
    in the prompt, the model picks alphabetically and we get the
    wrong Alina."""
    blob = OWNER_SYSTEM_PROMPT
    assert "DISAMBIGUATION" in blob or "disambiguat" in blob.lower()
    assert "role" in blob.lower()
    assert "notes" in blob.lower()


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
