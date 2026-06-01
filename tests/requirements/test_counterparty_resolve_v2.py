"""FR-CR-05-241 — SPEC + tests for v2 counterparty resolution and the
enrollment policy that fixes the prod directory pollution.

This module IS the specification for the safe rollout of pgvector+critic
counterparty matching:

  CONTRACT
    1. resolve_mentions_v2: each mention → pgvector top-K (retrieve_fn) →
       critic → V2Resolution(matched_entity_id|None, confidence,
       reasoning). PURE — no DB writes, no enrollment.
    2. ENROLLMENT POLICY: mentions_to_enroll(...) ALWAYS returns [].
       v2 never auto-enrolls an unresolved (garbled) mention as a new
       counterparty — the v1 behaviour that filled the directory with
       «Забалты»/«Не бучи»/«День Z». Unresolved are surfaced via
       partition() for review only.
    3. SHADOW: shadow_diff(v1, v2) yields agree/disagree/v1_only/v2_only/
       both_none counts for logging — no behaviour change.

  SAFETY: these are pure functions exercised with a FAKE retrieve_fn and
  FAKE critic backend — no network, no DB. Wiring into the live pipeline
  happens later behind COUNTERPARTY_MATCH_V2 (default off), shadow-first.
"""
from __future__ import annotations

from app.services.counterparty_resolve_v2 import (
    V2Resolution,
    mentions_to_enroll,
    partition,
    resolve_mentions_v2,
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
