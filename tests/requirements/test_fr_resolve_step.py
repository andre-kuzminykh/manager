"""FR-EC-CRITIC — resolve_for_meeting step: gating, shadow, apply, recording.
Everything injected (catalog/team/call/recorder) → no MCP / OpenAI / DB.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.services import entity_resolver_fr as R
from app.services.fr_resolve_step import resolve_for_meeting


@dataclass
class _S:
    entity_fr_resolver_enabled: bool = True
    entity_fr_resolver_shadow: bool = False
    entity_fr_mcp_url: str = "u"
    entity_fr_catalog_ttl_seconds: int = 3600
    entity_fr_max_context_tokens: int = 30000
    entity_fr_shard_workers: int = 2
    entity_fr_min_confidence: float = 0.7
    entity_fr_resolver_model: str = "gpt-5.5"


_CAT = [R.FrEntity(name="Key 1 Capital", entity_type="family_office", sources=["FO"])]


def _call_factory(decisions):
    def _call(_msgs):
        return list(decisions)
    return _call


# -- build_replacements (pure) ------------------------------------------------

def test_build_replacements_splits_variants_and_gates() -> None:
    decs = [
        R.Decision(mention="Ki One Capital India/Киван", canonical="Key 1 Capital", confidence=0.95),
        R.Decision(mention="weak", canonical="Whatever", confidence=0.4),       # below threshold
        R.Decision(mention="x", canonical=None, confidence=0.99),               # no canonical
        R.Decision(mention="Key 1 Capital", canonical="Key 1 Capital", confidence=0.9),  # identity
    ]
    repl = R.build_replacements(decs, min_confidence=0.7)
    assert repl == {"Ki One Capital India": "Key 1 Capital", "Киван": "Key 1 Capital"}


# -- step gating --------------------------------------------------------------

def test_disabled_returns_empty_and_does_nothing() -> None:
    rec = []
    out = resolve_for_meeting(
        settings=_S(entity_fr_resolver_enabled=False), text="t", meeting_title="m",
        participants=None, source="zoom", source_id="z1",
        catalog=_CAT, team_rows=[], call=_call_factory([]), recorder=lambda *a, **k: rec.append(k),
    )
    assert out == {} and rec == []


def test_apply_returns_replacements_and_records_applied() -> None:
    rec = []
    decisions = [R.Decision(mention="Киван", canonical="Key 1 Capital", confidence=0.95)]
    out = resolve_for_meeting(
        settings=_S(entity_fr_resolver_shadow=False), text="Обсудили Киван.",
        meeting_title="m", participants=["Alina"], source="zoom", source_id="z1",
        catalog=_CAT, team_rows=[], call=_call_factory(decisions),
        recorder=lambda d, **k: rec.append((d.mention, k["applied"], k["shadow"])),
    )
    assert out == {"Киван": "Key 1 Capital"}
    assert rec == [("Киван", True, False)]


def test_shadow_returns_empty_but_records_not_applied() -> None:
    rec = []
    decisions = [R.Decision(mention="Киван", canonical="Key 1 Capital", confidence=0.95)]
    out = resolve_for_meeting(
        settings=_S(entity_fr_resolver_shadow=True), text="Обсудили Киван.",
        meeting_title="m", participants=None, source="fireflies", source_id="f1",
        catalog=_CAT, team_rows=[], call=_call_factory(decisions),
        recorder=lambda d, **k: rec.append((d.mention, k["applied"], k["shadow"])),
    )
    assert out == {}                                  # shadow never changes text
    assert rec == [("Киван", False, True)]            # logged, not applied


def test_resolver_failure_is_swallowed() -> None:
    def _boom(_msgs):
        raise RuntimeError("llm down")
    out = resolve_for_meeting(
        settings=_S(), text="t", meeting_title="m", participants=None,
        source="zoom", source_id="z1", catalog=_CAT, team_rows=[], call=_boom,
        recorder=lambda *a, **k: None,
    )
    assert out == {}                                  # best-effort: never raises


def test_empty_catalog_returns_empty() -> None:
    out = resolve_for_meeting(
        settings=_S(), text="t", meeting_title="m", participants=None,
        source="zoom", source_id="z1", catalog=[], team_rows=[],
        call=_call_factory([R.Decision(mention="a", canonical="A", confidence=1.0)]),
        recorder=lambda *a, **k: None,
    )
    assert out == {}


__all__: list[str] = []
