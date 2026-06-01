"""FR-CR-05-241 — SPEC + tests for the v2 SHADOW hook.

The shadow hook is the SAFE first phase of the vector-catalog rollout. Its
contract is exactly its safety guarantees:

  1. NAME-BASED DIFF: v1 ids (prod directory) and v2 ids (catalog) live in
     different id-spaces, so the comparison is by normalised entity NAME —
     both / v1_only / v2_only.
  2. REUSES v1's mentions: no extra extract call; only embeddings + critic.
  3. NEVER WRITES: the hook touches no DB rows (it only reads the catalog).
  4. NEVER RAISES: any internal failure is swallowed and reported as None,
     so the canonical v1 pipeline can't be broken or slowed-failed by it.

Exercised with fakes — build_catalog_lexical_index / match_entity are
monkeypatched, so there is no network and no DB.
"""
from __future__ import annotations

from types import SimpleNamespace

import app.services.counterparty_shadow_v2 as shadow


class _Ent:
    def __init__(self, name: str):
        self.name = name


def _settings():
    return SimpleNamespace(
        openai_api_key="sk-test",
        embedding_model="text-embedding-3-large",
        entity_match_critic_model="gpt-4o",
        counterparty_match_v2_k=20,
    )


def _patch(monkeypatch, *, by_id, lex, verdicts):
    monkeypatch.setattr(shadow, "build_catalog_lexical_index",
                        lambda session: (lex, by_id))
    # embed fn is never actually exercised because match_entity is faked
    monkeypatch.setattr(shadow, "make_openai_embed_fn",
                        lambda *a, **k: (lambda text: [0.0]))
    monkeypatch.setattr(shadow, "search_entities",
                        lambda *a, **k: [])

    def fake_match_entity(*, mention, **_kw):
        return verdicts.get(mention, {"matched_entity_id": None})
    monkeypatch.setattr(shadow, "match_entity", fake_match_entity)


def test_no_api_key_is_a_safe_skip(monkeypatch):
    s = _settings()
    s.openai_api_key = None
    out = shadow.shadow_compare_v2(
        object(), settings=s, mentions=["X"], v1_canonical_names=[],
        transcript="X", source_kind="zoom", source_id="z1",
    )
    assert out is None


def test_name_based_diff_both_v1only_v2only(monkeypatch):
    by_id = {70: _Ent("Shorooq Partners"), 1219: _Ent("Mitsubishi Corporation"),
             1054: _Ent("CEVA Logistics")}
    lex = {}  # force everything through the critic path
    verdicts = {
        "Шрука": {"matched_entity_id": "70"},        # v2 catches, v1 had it too → both
        "Митсобиш": {"matched_entity_id": "1219"},   # v2 only (v1 missed)
        "Забалты": {"matched_entity_id": None},       # neither
    }
    _patch(monkeypatch, by_id=by_id, lex=lex, verdicts=verdicts)
    out = shadow.shadow_compare_v2(
        object(), settings=_settings(),
        mentions=["Шрука", "Митсобиш", "Забалты"],
        v1_canonical_names=["Shorooq Partners", "Aviva Investors"],
        transcript="...", source_kind="zoom", source_id="z1",
    )
    assert out is not None
    assert out["both"] == ["Shorooq Partners"]
    assert out["v2_only"] == ["Mitsubishi Corporation"]
    assert out["v1_only"] == ["Aviva Investors"]     # v1 had it, v2 didn't
    assert out["v2_critic"] == 2 and out["v2_none"] == 1


def test_exact_lexical_layer_counts_as_v2_match(monkeypatch):
    by_id = {1219: _Ent("Mitsubishi Corporation")}
    lex = {"mitsubishi": 1219}  # exact key (post-normalise)
    # match_entity must NOT be called for the exact hit
    def boom(**_kw):
        raise AssertionError("critic called for an exact-lexical hit")
    _patch(monkeypatch, by_id=by_id, lex={}, verdicts={})
    monkeypatch.setattr(shadow, "build_catalog_lexical_index",
                        lambda session: (lex, by_id))
    monkeypatch.setattr(shadow, "match_entity", boom)
    monkeypatch.setattr(shadow, "_normalise_name", lambda s: (s or "").lower())
    out = shadow.shadow_compare_v2(
        object(), settings=_settings(), mentions=["Mitsubishi"],
        v1_canonical_names=[], transcript="Mitsubishi",
        source_kind="zoom", source_id="z1",
    )
    assert out["v2_exact"] == 1
    assert out["v2_only"] == ["Mitsubishi Corporation"]


def test_invalid_critic_id_is_ignored(monkeypatch):
    by_id = {70: _Ent("Shorooq Partners")}
    verdicts = {"X": {"matched_entity_id": "999"}}  # id not in catalog
    _patch(monkeypatch, by_id=by_id, lex={}, verdicts=verdicts)
    out = shadow.shadow_compare_v2(
        object(), settings=_settings(), mentions=["X"],
        v1_canonical_names=[], transcript="X",
        source_kind="zoom", source_id="z1",
    )
    assert out["v2_none"] == 1 and out["v2_only"] == []


def test_never_raises_on_internal_error(monkeypatch):
    """The safety guarantee: an exploding dependency must NOT propagate."""
    def boom(session):
        raise RuntimeError("pgvector not installed")
    monkeypatch.setattr(shadow, "build_catalog_lexical_index", boom)
    out = shadow.shadow_compare_v2(
        object(), settings=_settings(), mentions=["X"],
        v1_canonical_names=[], transcript="X",
        source_kind="fireflies", source_id="f1",
    )
    assert out is None  # swallowed, pipeline unharmed


def test_empty_catalog_is_a_safe_skip(monkeypatch):
    _patch(monkeypatch, by_id={}, lex={}, verdicts={})
    out = shadow.shadow_compare_v2(
        object(), settings=_settings(), mentions=["X"],
        v1_canonical_names=[], transcript="X",
        source_kind="zoom", source_id="z1",
    )
    assert out is None
