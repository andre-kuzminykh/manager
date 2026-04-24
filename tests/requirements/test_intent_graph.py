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


def test_date_node_falls_back_to_resolver_when_llm_returns_null():
    backend = _StubBackend(
        detect={"is_task": True, "confidence": 0.9},
        title={"title": "t"},
        owner={"reasoning": "no", "display_name": None},
        date_={"due_date": None, "reasoning": "no date"},
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
