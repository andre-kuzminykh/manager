"""FR-CR-05-241 — SPEC + tests for the LIVE "on" resolver (v2 as the single
canonical counterparty mechanism).

CONTRACT
  resolve_mentions_to_directory_via_catalog(prod_session, *, settings,
      mentions, transcript, directory) -> {mention: counterparty_id | None}

  1. A catalog match whose normalised name EXISTS in the prod directory →
     that counterparty's id.
  2. A catalog match ABSENT from the directory → None (review; NEVER
     auto-created — the directory's source of truth is the Google Sheet, which
     would wipe an auto-created row).
  3. A mention with no catalog match → None.
  4. NEVER raises: a catalog/LLM failure yields all-None so the pipeline keeps
     running (no crash, no garbage).

Exercised with fakes — resolve_mentions_against_catalog is monkeypatched, so
there is no network and no DB.
"""
from __future__ import annotations

from types import SimpleNamespace

import app.services.counterparty_catalog_resolver as r


class _CP:
    def __init__(self, id: int, name_normalised: str):
        self.id = id
        self.name_normalised = name_normalised


def _settings():
    return SimpleNamespace(
        openai_api_key="sk-test",
        embedding_model="text-embedding-3-large",
        entity_match_critic_model="gpt-4o",
        counterparty_match_v2_k=20,
    )


def _patch_catalog(monkeypatch, matches):
    # catalog session resolves to the same DB (no separate factory) in tests
    monkeypatch.setattr(r, "open_catalog_session", lambda s: (s, False))
    monkeypatch.setattr(r, "resolve_mentions_against_catalog",
                        lambda session, **_kw: matches)
    # normalise = lowercase for predictable mapping in the test
    monkeypatch.setattr(r, "_normalise_name", lambda s: (s or "").strip().lower())


def test_links_existing_directory_entity(monkeypatch):
    matches = {"Митсобиш": {"entity_id": 1219, "name": "Mitsubishi", "method": "critic"}}
    _patch_catalog(monkeypatch, matches)
    directory = [_CP(50, "mitsubishi"), _CP(51, "nvidia")]
    out = r.resolve_mentions_to_directory_via_catalog(
        object(), settings=_settings(), mentions=["Митсобиш"],
        transcript="...", directory=directory,
    )
    assert out == {"Митсобиш": 50}  # mapped to the directory id by name


def test_absent_from_directory_is_none_not_created(monkeypatch):
    """Catalog matched a REAL entity, but it isn't in the 847 directory → None
    (review). The resolver must NOT invent an id."""
    matches = {"SpaceX": {"entity_id": 900, "name": "SpaceX", "method": "critic"}}
    _patch_catalog(monkeypatch, matches)
    directory = [_CP(50, "mitsubishi")]
    out = r.resolve_mentions_to_directory_via_catalog(
        object(), settings=_settings(), mentions=["SpaceX"],
        transcript="...", directory=directory,
    )
    assert out == {"SpaceX": None}


def test_alias_bridges_different_canonical_names(monkeypatch):
    """Catalog «NVIDIA Corporation» vs directory «Nvidia»: the name doesn't
    normalise-match, but the catalog entity's alias «Nvidia» bridges to the
    directory id."""
    matches = {"Nvidia": {"entity_id": 7, "name": "NVIDIA Corporation",
                          "aliases": "NVDA, Nvidia", "method": "critic"}}
    _patch_catalog(monkeypatch, matches)
    directory = [_CP(42, "nvidia")]
    out = r.resolve_mentions_to_directory_via_catalog(
        object(), settings=_settings(), mentions=["Nvidia"],
        transcript="...", directory=directory,
    )
    assert out == {"Nvidia": 42}  # linked via alias, not name


def test_no_catalog_match_is_none(monkeypatch):
    _patch_catalog(monkeypatch, {})  # nothing matched
    directory = [_CP(50, "mitsubishi")]
    out = r.resolve_mentions_to_directory_via_catalog(
        object(), settings=_settings(), mentions=["Забалты"],
        transcript="...", directory=directory,
    )
    assert out == {"Забалты": None}


def test_mixed_batch(monkeypatch):
    matches = {
        "Митсобиш": {"entity_id": 1219, "name": "Mitsubishi", "method": "critic"},
        "SpaceX": {"entity_id": 900, "name": "SpaceX", "method": "exact"},
    }
    _patch_catalog(monkeypatch, matches)
    directory = [_CP(50, "mitsubishi"), _CP(51, "nvidia")]
    out = r.resolve_mentions_to_directory_via_catalog(
        object(), settings=_settings(),
        mentions=["Митсобиш", "SpaceX", "Забалты"],
        transcript="...", directory=directory,
    )
    assert out == {"Митсобиш": 50, "SpaceX": None, "Забалты": None}


def test_never_raises_on_failure_returns_all_none(monkeypatch):
    """The safety guarantee: a catalog/LLM explosion must NOT propagate — the
    pipeline gets all-None and keeps running."""
    monkeypatch.setattr(r, "open_catalog_session", lambda s: (s, False))

    def boom(session, **_kw):
        raise RuntimeError("catalog unreachable")
    monkeypatch.setattr(r, "resolve_mentions_against_catalog", boom)
    out = r.resolve_mentions_to_directory_via_catalog(
        object(), settings=_settings(), mentions=["A", "B"],
        transcript="...", directory=[_CP(1, "a")],
    )
    assert out == {"A": None, "B": None}


def test_empty_mentions_short_circuits(monkeypatch):
    out = r.resolve_mentions_to_directory_via_catalog(
        object(), settings=_settings(), mentions=[],
        transcript="...", directory=[_CP(1, "a")],
    )
    assert out == {}


def test_owns_session_is_closed(monkeypatch):
    """When a separate catalog session is opened, it must be closed."""
    closed = {"rollback": 0, "close": 0}

    class _S:
        def rollback(self):
            closed["rollback"] += 1

        def close(self):
            closed["close"] += 1

    monkeypatch.setattr(r, "open_catalog_session", lambda s: (_S(), True))
    monkeypatch.setattr(r, "resolve_mentions_against_catalog",
                        lambda session, **_kw: {})
    monkeypatch.setattr(r, "_normalise_name", lambda s: (s or "").lower())
    r.resolve_mentions_to_directory_via_catalog(
        object(), settings=_settings(), mentions=["X"],
        transcript="...", directory=[],
    )
    assert closed == {"rollback": 1, "close": 1}
