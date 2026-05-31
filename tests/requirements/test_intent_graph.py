"""Requirement coverage: FR-CR-04-1 (detect node routes), FR-CR-04-2
(parallel extraction via LangGraph), FR-CR-04-3 (LLM-first date with
Python validator/fallback), NFR-CR-04-1 (per-node resilience).

LangGraph wiring:

    START ─▶ detect ─▶ is_task?
                        │
                        ├── false ──▶ assemble ─▶ END
                        │
                        └── true  ──▶ describe ┐
                                      owner    ├─▶ assemble ─▶ END
                                      date     ┘
"""
from __future__ import annotations

from datetime import date

from app.intent.date_prompt import DATE_TOOL_NAME
from app.intent.detect_prompt import DETECT_TOOL_NAME
from app.intent.owner_prompt import OWNER_TOOL_NAME
from app.intent.pipeline import run_pipeline
from app.intent.title_prompt import TITLE_TOOL_NAME
from app.schemas.intent import IntentType


class _StubBackend:
    """Records every tool call and dispatches canned payloads per stage."""

    def __init__(self, *, detect=None, title=None, owner=None, date_=None):
        self._payloads = {
            DETECT_TOOL_NAME: detect,
            TITLE_TOOL_NAME: title,
            OWNER_TOOL_NAME: owner,
            DATE_TOOL_NAME: date_,
        }
        self.calls: list[dict] = []

    def extract_intent(self, *, user_prompt):  # pragma: no cover
        raise NotImplementedError

    def call_tool(
        self,
        *,
        system_prompt,
        user_prompt,
        tool_name,
        tool_description,
        tool_parameters,
        model=None,
    ):
        self.calls.append({"tool_name": tool_name, "model": model})
        return self._payloads.get(tool_name)


TODAY = date(2026, 4, 24)  # Friday


# -------- Routing: detect=false short-circuits the graph ------------------ #


def test_detect_false_skips_extraction_nodes():
    backend = _StubBackend(
        detect={"is_task": False, "confidence": 0.05, "reasoning": "chat"},
        title={"title": "unused"},
        owner={"reasoning": "unused", "display_name": None},
        date_={"due_date": None, "reasoning": "unused"},
    )
    out = run_pipeline(
        backend=backend,
        source_text="привет всем",
        context_messages=[],
        author_user_id="U-author",
        today=TODAY,
    )
    assert out.intent == IntentType.no_action
    called = {c["tool_name"] for c in backend.calls}
    assert called == {DETECT_TOOL_NAME}


# -------- Routing: detect=true fans out to all three extractors ----------- #


def test_detect_true_fans_out_to_all_three_extractors():
    backend = _StubBackend(
        detect={"is_task": True, "confidence": 0.9},
        title={"title": "сделать отчёт"},
        owner={"reasoning": "no one", "display_name": None},
        date_={"due_date": "2026-05-01", "reasoning": "к 1 мая"},
    )
    out = run_pipeline(
        backend=backend,
        source_text="надо сделать отчёт к 1 мая",
        context_messages=[],
        author_user_id="U-author",
        today=TODAY,
    )
    called = [c["tool_name"] for c in backend.calls]
    assert DETECT_TOOL_NAME in called
    assert TITLE_TOOL_NAME in called
    assert OWNER_TOOL_NAME in called
    assert DATE_TOOL_NAME in called
    assert out.intent == IntentType.create_task
    assert out.task.title == "сделать отчёт"
    assert out.task.due_date == date(2026, 5, 1)


# -------- Date node: LLM wins when it returns a valid future ISO ---------- #


def test_date_node_uses_llm_answer_when_future_iso():
    backend = _StubBackend(
        detect={"is_task": True, "confidence": 0.9},
        title={"title": "t"},
        owner={"reasoning": "no", "display_name": None},
        date_={"due_date": "2026-05-12", "reasoning": "explicit"},
    )
    out = run_pipeline(
        backend=backend,
        source_text="сделать к 12 мая",
        context_messages=[],
        author_user_id="U",
        today=TODAY,
    )
    assert out.task.due_date == date(2026, 5, 12)


# -------- Date node: LLM null → Python fallback fills in ------------------ #


def test_date_node_does_not_fall_back_when_llm_intentionally_null():
    """FR-CR-05-101 (updated by FR-CR-05-218) — operator regression:
    LLM correctly judged a context date as not-a-deadline
    (FR-CR-05-87/89), returned null + reasoning. The python date
    resolver is SKIPPED whenever the LLM gave a non-empty reasoning
    (signal that the null is intentional). FR-CR-05-218 then adds a
    final today-fallback so the operator never sees a None deadline
    — the task lands on today rather than re-extracting the date
    the LLM intentionally rejected."""
    backend = _StubBackend(
        detect={"is_task": True, "confidence": 0.9},
        title={"title": "t"},
        owner={"reasoning": "no", "display_name": None},
        date_={
            "due_date": None,
            "reasoning": "no explicit deadline; «к понедельнику» is …",
        },
    )
    out = run_pipeline(
        backend=backend,
        source_text="надо сделать к понедельнику",
        context_messages=[],
        author_user_id="U",
        today=TODAY,
    )
    # LLM intentionally said null with reasoning → python fallback
    # skipped, BUT FR-CR-05-218 today-fallback lands on TODAY (the
    # date the LLM REJECTED — «к понедельнику» — is NOT re-extracted).
    assert out.task.due_date == TODAY


def test_date_node_falls_back_when_llm_silent_no_reasoning():
    """FR-CR-05-101 — fallback still runs when the LLM was
    silent (no reasoning either) — that suggests the call
    failed or returned malformed data, not an intentional
    null."""
    backend = _StubBackend(
        detect={"is_task": True, "confidence": 0.9},
        title={"title": "t"},
        owner={"reasoning": "no", "display_name": None},
        date_={"due_date": None, "reasoning": ""},
    )
    out = run_pipeline(
        backend=backend,
        source_text="надо сделать к понедельнику",
        context_messages=[],
        author_user_id="U",
        today=TODAY,
    )
    # Monday after Friday 2026-04-24 is 2026-04-27.
    assert out.task.due_date == date(2026, 4, 27)


# -------- Date node: back-dated LLM answer is rejected -------------------- #


def test_date_node_rejects_llm_past_date_without_today_hint():
    backend = _StubBackend(
        detect={"is_task": True, "confidence": 0.9},
        title={"title": "t"},
        owner={"reasoning": "no", "display_name": None},
        date_={"due_date": "2026-04-01", "reasoning": "hallucinated past"},
    )
    out = run_pipeline(
        backend=backend,
        source_text="сделать к следующей пятнице",
        context_messages=[],
        author_user_id="U",
        today=TODAY,
    )
    # LLM's past date is rejected; resolver sees "к следующей пятнице"
    # — the "пятниц" stem matches and resolver returns the upcoming
    # Friday (2026-05-01).
    assert out.task.due_date == date(2026, 5, 1)


def test_date_node_accepts_today_when_user_explicitly_wrote_today():
    backend = _StubBackend(
        detect={"is_task": True, "confidence": 0.9},
        title={"title": "t"},
        owner={"reasoning": "no", "display_name": None},
        date_={"due_date": "2026-04-24", "reasoning": "today"},
    )
    out = run_pipeline(
        backend=backend,
        source_text="сделать сегодня",
        context_messages=[],
        author_user_id="U",
        today=TODAY,
    )
    assert out.task.due_date == TODAY


# -------- Date node: garbage from LLM → resolver rescues ------------------ #


def test_date_node_falls_back_when_llm_returns_garbage():
    backend = _StubBackend(
        detect={"is_task": True, "confidence": 0.9},
        title={"title": "t"},
        owner={"reasoning": "no", "display_name": None},
        date_={"due_date": "maybe-next-friday", "reasoning": "bad format"},
    )
    out = run_pipeline(
        backend=backend,
        source_text="надо через неделю",
        context_messages=[],
        author_user_id="U",
        today=TODAY,
    )
    # Resolver catches "через неделю" → +7 days.
    assert out.task.due_date == date(2026, 5, 1)


# -------- Model override: date node uses the dedicated model -------------- #


def test_date_node_uses_configured_date_model():
    backend = _StubBackend(
        detect={"is_task": True, "confidence": 0.9},
        title={"title": "t"},
        owner={"reasoning": "no", "display_name": None},
        date_={"due_date": None, "reasoning": "no"},
    )
    run_pipeline(
        backend=backend,
        source_text="надо сделать",
        context_messages=[],
        author_user_id="U",
        today=TODAY,
        date_model="gpt-4o-2024-08-06",
    )
    date_call = next(c for c in backend.calls if c["tool_name"] == DATE_TOOL_NAME)
    assert date_call["model"] == "gpt-4o-2024-08-06"
    # Detect and other nodes did NOT receive a model override.
    detect_call = next(c for c in backend.calls if c["tool_name"] == DETECT_TOOL_NAME)
    assert detect_call["model"] is None


# -------- Resilience: a failing node doesn't abort the graph -------------- #


def test_owner_node_failure_leaves_owner_null_but_keeps_classification():
    class _BrokenOwner(_StubBackend):
        def call_tool(self, **kw):
            if kw["tool_name"] == OWNER_TOOL_NAME:
                raise RuntimeError("openai down")
            return super().call_tool(**kw)

    backend = _BrokenOwner(
        detect={"is_task": True, "confidence": 0.9},
        title={"title": "сделать"},
        owner={"reasoning": "unused", "display_name": None},
        date_={"due_date": None, "reasoning": "none"},
    )
    out = run_pipeline(
        backend=backend,
        source_text="надо сделать X",
        context_messages=[],
        author_user_id="U",
        today=TODAY,
    )
    assert out.intent == IntentType.create_task
    assert out.task.owner_user_id is None
    assert out.task.title == "сделать"


def test_date_node_failure_falls_back_to_resolver():
    class _BrokenDate(_StubBackend):
        def call_tool(self, **kw):
            if kw["tool_name"] == DATE_TOOL_NAME:
                raise RuntimeError("openai timeout")
            return super().call_tool(**kw)

    backend = _BrokenDate(
        detect={"is_task": True, "confidence": 0.9},
        title={"title": "t"},
        owner={"reasoning": "no", "display_name": None},
        date_=None,
    )
    out = run_pipeline(
        backend=backend,
        source_text="надо сделать завтра",
        context_messages=[],
        author_user_id="U",
        today=TODAY,
    )
    assert out.task.due_date == date(2026, 4, 25)


def test_detect_node_failure_yields_no_action():
    class _BrokenDetect(_StubBackend):
        def call_tool(self, **kw):
            if kw["tool_name"] == DETECT_TOOL_NAME:
                raise RuntimeError("openai")
            return super().call_tool(**kw)

    backend = _BrokenDetect()
    out = run_pipeline(
        backend=backend,
        source_text="надо сделать",
        context_messages=[],
        author_user_id="U",
        today=TODAY,
    )
    # No recovery at pipeline level — classify_with_backend sits above
    # and runs the prefilter override. The graph alone returns no_action.
    assert out.intent == IntentType.no_action


def test_fr_cr_05_218_today_fallback_when_no_anchor_anywhere():
    """FR-CR-05-218 — operator: «дедлайн должен стоять в любом случае».
    When the LLM emits null AND there's no temporal anchor anywhere
    (source, no chat history), the pipeline lands on TODAY rather
    than None."""
    backend = _StubBackend(
        detect={"is_task": True, "confidence": 0.9},
        title={"title": "сделай отчёт"},
        owner={"reasoning": "no", "display_name": None},
        date_={"due_date": None, "reasoning": "no date stated"},
    )
    out = run_pipeline(
        backend=backend,
        source_text="надо сделать отчёт",
        context_messages=[],
        author_user_id="U",
        today=TODAY,
    )
    assert out.task is not None
    # Operator: never leave a draft with due_date=None.
    assert out.task.due_date == TODAY


def test_fr_cr_05_218_build_date_user_prompt_passes_wider_source():
    """FR-CR-05-218 — date-stage user prompt now carries
    `wider_source` and `prior_context` blocks so multi-task chunks
    can resolve dependencies that live OUTSIDE the chunk itself."""
    from app.intent.date_prompt import build_date_user_prompt

    prompt = build_date_user_prompt(
        source_text="пусть Андрей поговорит с Олегом 20 мин",
        current_date="2026-05-31",
        current_weekday="Sunday",
        wider_source=(
            "Ир поставь встречу в пон или вт обсудим "
            "А до этого пусть Андрей поговорит с Олегом 20 мин"
        ),
        prior_context=[{"user": "U-author", "text": "контекст выше"}],
    )
    assert "wider_source" in prompt
    assert "Ир поставь встречу" in prompt  # the cross-chunk anchor
    assert "prior_context" in prompt
    assert "контекст выше" in prompt
    # The chunk itself is still labelled clearly.
    assert "пусть Андрей поговорит" in prompt


def test_fr_cr_05_218_date_prompt_teaches_preparation_dependency():
    """FR-CR-05-218 — DATE_SYSTEM_PROMPT must teach the dependency
    pattern «а до этого пусть X сделает Y» = deadline ≤ dependent
    task's date. Pinned worked example from snapshot 4965."""
    from app.intent.date_prompt import DATE_SYSTEM_PROMPT

    blob = DATE_SYSTEM_PROMPT
    assert "FR-CR-05-218" in blob
    assert "CONTEXT-AWARE DEADLINES" in blob
    # The exact dependency phrase patterns are pinned.
    assert "а до этого" in blob.lower() or "до этого" in blob.lower()
    # Worked example from the operator regression is pinned.
    assert "Ир поставь встречу" in blob
    assert "Андрей поговорит" in blob
