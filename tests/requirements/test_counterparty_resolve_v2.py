"""FR-CR-05-241 — SPEC + tests for v2 counterparty resolution against the
**4000-entity catalog** (operator 2026-06-01: «мы делаем для 4000»).

TARGET = pure RAG: extract (gpt-5.5 ×1) → pgvector top-K (k≈20) → critic
gpt-4o → matched | REVIEW QUEUE. A 4000-name directory CANNOT be stuffed
into an LLM prompt, so the legacy whole-dir resolve is impossible here;
the only knobs on recall are k / aliases / extract quality. This module
IS the specification:

  CONTRACT
    1. resolve_mentions_v2: each mention → pgvector top-K (retrieve_fn) →
       critic → V2Resolution(matched_entity_id|None, confidence,
       reasoning). PURE — no DB writes, no enrollment. Default k =
       CATALOG_DEFAULT_K (=20, the 4000-target recall width).
    2. ENROLLMENT POLICY: mentions_to_enroll(...) ALWAYS returns [].
       v2 never auto-enrolls an unresolved (garbled) mention as a new
       counterparty — the v1 behaviour that filled the directory with
       «Забалты»/«Не бучи»/«День Z». Unresolved go to review_queue(...)
       (and are also surfaced via partition()).
    3. REVIEW QUEUE: review_queue(...) returns one curatable row per
       unresolved mention (surface_form, confidence, reasoning, method),
       de-duplicated — the operator-curated replacement for auto-enroll.
    4. SHADOW: shadow_diff(v1, v2) yields agree/disagree/v1_only/v2_only/
       both_none counts for logging — no behaviour change.

  NOTE: resolve_mentions_hybrid (v1 whole-dir fallback) is **847-only
  legacy** — kept for the small-directory scenario, NOT the 4000 target.

  SAFETY: these are pure functions exercised with a FAKE retrieve_fn and
  FAKE critic backend — no network, no DB. Wiring into the live pipeline
  happens later behind COUNTERPARTY_MATCH_V2 (default off), shadow-first.
"""
from __future__ import annotations

from app.services.counterparty_resolve_v2 import (
    CATALOG_DEFAULT_K,
    V2Resolution,
    mentions_to_enroll,
    partition,
    resolve_mentions_hybrid,
    resolve_mentions_v2,
    review_queue,
    shadow_diff,
)


class _FakeBackend:
    """Critic stub: returns a scripted verdict per mention via call_tool."""

    def __init__(self, verdicts: dict[str, dict]):
        self._v = verdicts
        self.calls = 0

    def call_tool(self, *, user_prompt: str, **_kw) -> dict:
        self.calls += 1
        # user_prompt starts with "mention: <m>"; find which mention it is
        first = user_prompt.splitlines()[0]
        mention = first.split("mention:", 1)[-1].strip()
        return self._v.get(mention, {"matched_entity_id": None,
                                     "confidence": 0.3, "reasoning": "no fit"})


def _retrieve_factory(by_mention: dict[str, list[dict]]):
    def fn(query: str, k: int):
        # query = "mention. context"; take the mention part
        m = query.split(".", 1)[0].strip()
        return by_mention.get(m, [])[:k]
    return fn


def test_resolves_match_and_none_without_side_effects():
    cands = {
        "Митсобиш": [{"entity_id": "1219", "text_repr": "Mitsubishi Corporation", "score": 0.7}],
        "Забалты": [{"entity_id": "192", "text_repr": "Jimco", "score": 0.4}],
    }
    backend = _FakeBackend({
        "Митсобиш": {"matched_entity_id": "1219", "confidence": 0.9, "reasoning": "same name"},
        "Забалты": {"matched_entity_id": None, "confidence": 0.3, "reasoning": "not the same name"},
    })
    res = resolve_mentions_v2(
        mentions=["Митсобиш", "Забалты"],
        retrieve_fn=_retrieve_factory(cands), backend=backend,
    )
    by = {r.mention: r for r in res}
    assert by["Митсобиш"].matched_entity_id == 1219
    assert by["Забалты"].matched_entity_id is None
    assert backend.calls == 2  # one critic call per mention


def test_enrollment_policy_never_enrolls_unresolved():
    """The precision-critical guarantee: garbled none → NOT enrolled."""
    res = [
        V2Resolution("Митсобиш", 1219, 0.9, ""),
        V2Resolution("Забалты", None, 0.3, ""),
        V2Resolution("Не бучи", None, 0.3, ""),
        V2Resolution("День Z", None, 0.3, ""),
    ]
    assert mentions_to_enroll(res) == []  # ZERO auto-enroll under v2

    matched, unresolved = partition(res)
    assert [m.matched_entity_id for m in matched] == [1219]
    assert unresolved == ["Забалты", "Не бучи", "День Z"]  # surfaced, not written


def test_review_queue_holds_unresolved_for_curation_not_catalog():
    """4000-target: unresolved → review_queue rows (operator curates),
    matched are excluded, and NOTHING is enrolled."""
    res = [
        V2Resolution("Митсобиш", 1219, 0.92, "same name"),
        V2Resolution("Забалты", None, 0.30, "no same-name candidate"),
        V2Resolution("сугу", None, 0.25, "Sugo != Genia"),
    ]
    q = review_queue(res)
    forms = {r["surface_form"] for r in q}
    assert forms == {"Забалты", "сугу"}          # matched excluded
    assert all("matched_entity_id" not in r for r in q)
    assert {r["reasoning"] for r in q} == {"no same-name candidate", "Sugo != Genia"}
    assert mentions_to_enroll(res) == []          # still zero auto-enroll


def test_review_queue_dedups_keeping_strongest_near_miss():
    res = [
        V2Resolution("Маслон", None, 0.20, "weak"),
        V2Resolution("Маслон", None, 0.55, "closer near-miss"),  # higher conf kept
    ]
    q = review_queue(res)
    assert len(q) == 1
    assert q[0]["confidence"] == 0.55
    assert q[0]["reasoning"] == "closer near-miss"


def test_catalog_default_k_is_20():
    """The 4000-target recall width the operator raised 12→20."""
    assert CATALOG_DEFAULT_K == 20


def test_invalid_critic_id_is_dropped_to_none():
    """If the critic returns an id NOT among candidates, entity_match_v2
    guards it to None → v2 must surface None (never a phantom link)."""
    cands = {"X": [{"entity_id": "10", "text_repr": "Acme", "score": 0.5}]}
    backend = _FakeBackend({"X": {"matched_entity_id": "999", "confidence": 0.9, "reasoning": "hallucinated"}})
    res = resolve_mentions_v2(mentions=["X"], retrieve_fn=_retrieve_factory(cands), backend=backend)
    assert res[0].matched_entity_id is None


def test_no_candidates_yields_none():
    backend = _FakeBackend({})
    res = resolve_mentions_v2(mentions=["Ghost"], retrieve_fn=_retrieve_factory({}), backend=backend)
    assert res[0].matched_entity_id is None


def test_hybrid_falls_back_to_v1_only_for_v2_none():
    """Recall-safe contract: v2 wins are kept; v1 LLM is invoked ONLY on
    the mentions v2 returned none for (efficiency), and recovers the
    speech-garbled ones v2 missed («хабспот»→HubSpot)."""
    cands = {
        "20VC": [{"entity_id": "55", "text_repr": "20VC", "score": 0.8}],
        "хабспот": [{"entity_id": "70", "text_repr": "Shorooq", "score": 0.3}],  # wrong top
    }
    backend = _FakeBackend({
        "20VC": {"matched_entity_id": "55", "confidence": 0.9, "reasoning": "same"},
        "хабспот": {"matched_entity_id": None, "confidence": 0.3, "reasoning": "no same-name"},
    })

    v1_calls = {}
    def v1_resolve(none_mentions):
        v1_calls["arg"] = list(none_mentions)
        # v1 whole-dir LLM recovers the garbled HubSpot
        return {"хабспот": 1404}

    res = resolve_mentions_hybrid(
        mentions=["20VC", "хабспот"],
        retrieve_fn=_retrieve_factory(cands), backend=backend,
        v1_resolve_fn=v1_resolve,
    )
    by = {r.mention: r for r in res}
    assert by["20VC"].matched_entity_id == 55 and by["20VC"].method == "v2"
    assert by["хабспот"].matched_entity_id == 1404 and by["хабспот"].method == "v1_fallback"
    # v1 called ONLY for the none-set (not for 20VC)
    assert v1_calls["arg"] == ["хабспот"]


def test_hybrid_still_none_when_both_fail_and_not_enrolled():
    cands = {"Забалты": [{"entity_id": "192", "text_repr": "Jimco", "score": 0.3}]}
    backend = _FakeBackend({"Забалты": {"matched_entity_id": None, "confidence": 0.3, "reasoning": "no"}})
    res = resolve_mentions_hybrid(
        mentions=["Забалты"], retrieve_fn=_retrieve_factory(cands), backend=backend,
        v1_resolve_fn=lambda ms: {m: None for m in ms},  # v1 also can't
    )
    assert res[0].matched_entity_id is None and res[0].method == "none"
    assert mentions_to_enroll(res) == []  # garbage still NOT enrolled


def test_hybrid_skips_v1_entirely_when_v2_resolves_all():
    cands = {"20VC": [{"entity_id": "55", "text_repr": "20VC", "score": 0.9}]}
    backend = _FakeBackend({"20VC": {"matched_entity_id": "55", "confidence": 0.95, "reasoning": "same"}})
    called = {"n": 0}
    def v1(ms):
        called["n"] += 1
        return {}
    res = resolve_mentions_hybrid(mentions=["20VC"], retrieve_fn=_retrieve_factory(cands),
                                  backend=backend, v1_resolve_fn=v1)
    assert res[0].matched_entity_id == 55
    assert called["n"] == 0  # no none-set → v1 not called at all (cost saved)


def test_shadow_diff_counts_and_samples():
    v1 = {"Митсобиш": 1219, "Забалты": 192, "Севе": 1054, "New": None, "Шрука": 70}
    v2 = [
        V2Resolution("Митсобиш", 1219, 0.9, ""),   # agree
        V2Resolution("Забалты", None, 0.3, ""),    # v1_only (v1 enrolled garbage, v2 none)
        V2Resolution("Севе", 1054, 0.9, ""),       # agree
        V2Resolution("New", 500, 0.8, ""),         # v2_only
        V2Resolution("Шрука", 71, 0.8, ""),        # disagree_id
    ]
    d = shadow_diff(v1, v2)
    assert d["total"] == 5
    assert d["agree"] == 2
    assert d["v1_only"] == 1
    assert d["v2_only"] == 1
    assert d["disagree_id"] == 1
    assert d["both_none"] == 0
    assert any(s["mention"] == "Забалты" for s in d["samples"])
