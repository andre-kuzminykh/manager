"""FR-CR-05-222 — agentic entity matcher (LangGraph critic subgraph).

Stubs retrieval (pgvector) and the LLM backend so the graph logic is
tested without a DB or OpenAI: confident match, low-confidence
disambiguation (widen K + re-critique), invalid-id guard, and the
no-candidates short-circuit.
"""
from __future__ import annotations

from app.services.entity_match_v2 import (
    CONF_THRESHOLD,
    MAX_ATTEMPTS,
    WIDEN_K,
    build_critic_user_prompt,
    match_entity,
)


class _StubBackend:
    """Returns a queued critic verdict per call (one per critique node)."""

    def __init__(self, verdicts):
        self._verdicts = list(verdicts)
        self.calls = []

    def call_tool(self, **kw):
        self.calls.append(kw)
        return self._verdicts.pop(0) if self._verdicts else {}


def _retrieve_factory(by_k):
    """by_k: dict {k: [candidates]} — returns candidates for the asked k.
    Records the k values it was called with."""
    seen_ks = []

    def fn(query, k):
        seen_ks.append(k)
        return by_k.get(k, by_k.get("default", []))

    fn.seen_ks = seen_ks  # type: ignore[attr-defined]
    return fn


# ------------------------------------------------------------------ #
def test_confident_match_single_pass():
    cands = [
        {"entity_id": "10", "text_repr": "ADNOC. sector: oil", "score": 0.91},
        {"entity_id": "20", "text_repr": "Adnoc Drilling", "score": 0.77},
    ]
    backend = _StubBackend([
        {"matched_entity_id": "10", "confidence": 0.92, "reasoning": "«Адног» → ADNOC"},
    ])
    fn = _retrieve_factory({"default": cands})
    out = match_entity(
        kind="counterparty", mention="Адног", context="нефтянка из ОАЭ",
        retrieve_fn=fn, backend=backend,
    )
    assert out["matched_entity_id"] == "10"
    assert out["confidence"] == 0.92
    assert out["attempts"] == 1  # confident → no disambiguation
    assert len(backend.calls) == 1
    # retrieved once at default K (10)
    assert fn.seen_ks == [10]


def test_low_confidence_triggers_disambiguation_then_resolves():
    narrow = [{"entity_id": "1", "text_repr": "Mistral AI", "score": 0.55}]
    wide = [
        {"entity_id": "1", "text_repr": "Mistral AI", "score": 0.55},
        {"entity_id": "2", "text_repr": "Mistral Capital. fund", "score": 0.71},
    ]
    backend = _StubBackend([
        # first pass: unsure
        {"matched_entity_id": "1", "confidence": 0.4, "reasoning": "maybe"},
        # second pass after widen: confident on the fund
        {"matched_entity_id": "2", "confidence": 0.85, "reasoning": "context = fund → Mistral Capital"},
    ])
    fn = _retrieve_factory({10: narrow, WIDEN_K: wide})
    out = match_entity(
        kind="counterparty", mention="Мистраль", context="инвестфонд",
        retrieve_fn=fn, backend=backend,
    )
    assert out["matched_entity_id"] == "2"
    assert out["confidence"] == 0.85
    assert out["attempts"] == 2
    assert len(backend.calls) == 2
    # retrieved at default K then widened
    assert fn.seen_ks == [10, WIDEN_K]


def test_low_confidence_gives_up_after_max_attempts():
    cands = [{"entity_id": "9", "text_repr": "X", "score": 0.3}]
    # critic stays unsure both passes
    backend = _StubBackend([
        {"matched_entity_id": None, "confidence": 0.2, "reasoning": "weak"},
        {"matched_entity_id": None, "confidence": 0.25, "reasoning": "still weak"},
    ])
    fn = _retrieve_factory({"default": cands})
    out = match_entity(
        kind="counterparty", mention="???", context="",
        retrieve_fn=fn, backend=backend,
    )
    assert out["matched_entity_id"] is None
    assert out["attempts"] == MAX_ATTEMPTS
    assert len(backend.calls) == MAX_ATTEMPTS


def test_invalid_id_from_critic_is_rejected():
    cands = [{"entity_id": "10", "text_repr": "ADNOC", "score": 0.9}]
    # critic hallucinates an id not in candidates → must be nulled out;
    # high confidence so we don't loop.
    backend = _StubBackend([
        {"matched_entity_id": "999", "confidence": 0.95, "reasoning": "hallucinated"},
    ])
    fn = _retrieve_factory({"default": cands})
    out = match_entity(
        kind="counterparty", mention="x", context="",
        retrieve_fn=fn, backend=backend,
    )
    assert out["matched_entity_id"] is None  # invalid id rejected


def test_no_candidates_short_circuits_without_llm():
    backend = _StubBackend([])  # would raise IndexError if called
    fn = _retrieve_factory({"default": []})
    out = match_entity(
        kind="counterparty", mention="ghost", context="",
        retrieve_fn=fn, backend=backend,
    )
    assert out["matched_entity_id"] is None
    assert out["confidence"] == 0.0
    assert backend.calls == []  # critic never invoked


def test_result_is_auditable():
    """The result must carry candidates + reasoning so the review
    harness (FR-CR-05-224) can show «извлекли X → подставили Y, почему»."""
    cands = [{"entity_id": "10", "text_repr": "ADNOC", "score": 0.9}]
    backend = _StubBackend([
        {"matched_entity_id": "10", "confidence": 0.9, "reasoning": "clear"},
    ])
    fn = _retrieve_factory({"default": cands})
    out = match_entity(
        kind="counterparty", mention="Адног", context="oil",
        retrieve_fn=fn, backend=backend,
    )
    assert out["mention"] == "Адног"
    assert out["kind"] == "counterparty"
    assert out["candidates"] == cands
    assert out["reasoning"] == "clear"


def test_critic_prompt_lists_candidates_and_forbids_invented_ids():
    p = build_critic_user_prompt(
        mention="Адног", context="нефтянка",
        candidates=[{"entity_id": "10", "text_repr": "ADNOC oil", "score": 0.9}],
    )
    assert "Адног" in p
    assert "нефтянка" in p
    assert "10" in p and "ADNOC oil" in p


def test_threshold_constant_sane():
    assert 0.0 < CONF_THRESHOLD < 1.0
    assert MAX_ATTEMPTS >= 1
