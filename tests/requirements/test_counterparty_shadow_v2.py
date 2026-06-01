"""FR-CR-05-241 — SPEC + tests for the v2 SHADOW hook.

The shadow hook is the observe-only phase of the vector-catalog rollout. Its
contract is exactly its safety guarantees:

  1. NAME-BASED DIFF: v1 ids (prod directory) and v2 ids (catalog) live in
     different id-spaces, so the comparison is by normalised entity NAME —
     both / v1_only / v2_only.
  2. REUSES v1's mentions: no extra extract call; only embeddings + critic.
  3. NEVER WRITES: the hook touches no DB rows (it only reads the catalog).
  4. NEVER RAISES: any internal failure is swallowed and reported as None,
     so the canonical pipeline can't be broken or slowed-failed by it.

Shadow shares its catalog resolution with the live "on" path via
counterparty_catalog_resolver, so these tests monkeypatch that seam
(open_catalog_session / resolve_mentions_against_catalog) — no network, no DB.
"""
from __future__ import annotations

from types import SimpleNamespace

import app.services.counterparty_shadow_v2 as shadow


def _settings():
    return SimpleNamespace(
        openai_api_key="sk-test",
        embedding_model="text-embedding-3-large",
        entity_match_critic_model="gpt-4o",
        counterparty_match_v2_k=20,
    )


def _patch(monkeypatch, matches, *, session=None, owns=False):
    monkeypatch.setattr(shadow, "open_catalog_session",
                        lambda s: (session if session is not None else s, owns))
    monkeypatch.setattr(shadow, "resolve_mentions_against_catalog",
                        lambda cat_session, **_kw: matches)
    monkeypatch.setattr(shadow, "_normalise_name",
                        lambda s: (s or "").strip().lower())


def test_no_api_key_is_a_safe_skip(monkeypatch):
    s = _settings()
    s.openai_api_key = None
    out = shadow.shadow_compare_v2(
        object(), settings=s, mentions=["X"], v1_canonical_names=[],
        transcript="X", source_kind="zoom", source_id="z1",
    )
    assert out is None


def test_name_based_diff_both_v1only_v2only(monkeypatch):
    matches = {
        "Шрука": {"entity_id": 70, "name": "Shorooq Partners", "method": "critic"},
        "Митсобиш": {"entity_id": 1219, "name": "Mitsubishi Corporation", "method": "critic"},
        # "Забалты" → no match (omitted)
    }
    _patch(monkeypatch, matches)
    out = shadow.shadow_compare_v2(
        object(), settings=_settings(),
        mentions=["Шрука", "Митсобиш", "Забалты"],
        v1_canonical_names=["Shorooq Partners", "Aviva Investors"],
        transcript="...", source_kind="zoom", source_id="z1",
    )
    assert out is not None
    assert out["both"] == ["Shorooq Partners"]
    assert out["v2_only"] == ["Mitsubishi Corporation"]
    assert out["v1_only"] == ["Aviva Investors"]
    assert out["v2_critic"] == 2 and out["v2_none"] == 1


def test_method_counts_exact_vs_critic(monkeypatch):
    matches = {
        "Tether": {"entity_id": 188, "name": "Tether", "method": "exact"},
        "Митсобиш": {"entity_id": 1219, "name": "Mitsubishi", "method": "critic"},
    }
    _patch(monkeypatch, matches)
    out = shadow.shadow_compare_v2(
        object(), settings=_settings(), mentions=["Tether", "Митсобиш", "Ghost"],
        v1_canonical_names=[], transcript="...",
        source_kind="zoom", source_id="z1",
    )
    assert out["v2_exact"] == 1 and out["v2_critic"] == 1 and out["v2_none"] == 1


def test_never_raises_on_internal_error(monkeypatch):
    """The safety guarantee: an exploding dependency must NOT propagate."""
    monkeypatch.setattr(shadow, "open_catalog_session", lambda s: (s, False))

    def boom(cat_session, **_kw):
        raise RuntimeError("pgvector unreachable")
    monkeypatch.setattr(shadow, "resolve_mentions_against_catalog", boom)
    out = shadow.shadow_compare_v2(
        object(), settings=_settings(), mentions=["X"],
        v1_canonical_names=[], transcript="X",
        source_kind="fireflies", source_id="f1",
    )
    assert out is None  # swallowed, pipeline unharmed


def test_borrowed_catalog_session_is_closed(monkeypatch):
    """When a separate catalog session is opened (owns=True) it must be
    rolled back + closed — the prod-DB-untouched / no-leak guarantee."""
    closed = {"rollback": 0, "close": 0}

    class _CatSession:
        def rollback(self):
            closed["rollback"] += 1

        def close(self):
            closed["close"] += 1

    _patch(monkeypatch, {}, session=_CatSession(), owns=True)
    out = shadow.shadow_compare_v2(
        object(), settings=_settings(), mentions=["X"],
        v1_canonical_names=[], transcript="X",
        source_kind="zoom", source_id="z1",
    )
    assert out is not None  # empty catalog → diff with v2 finding nothing
    assert closed == {"rollback": 1, "close": 1}
