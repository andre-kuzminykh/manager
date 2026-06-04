"""FR-EC-CRITIC — pure-function tests for the FR entity resolver. Built on a
REAL sample of Viktor's humanoid_fr_search MCP output (incl. the JSON wrapper,
"Found N results:" header, duplicate source sheets, missing optional fields).
No network: fetch_catalog takes an injected call_tool; the LLM is never called.
"""
from __future__ import annotations

import json

from app.services import entity_resolver_fr as r

# One real-shape MCP blob: the search text is wrapped in [{"response": "..."}].
_SAMPLE_INNER = (
    "Found 3 results:\n\n"
    "1. Charles Button | ID: f041c458-ade8-4ec3-a7d1-f59e4e7c8f0f | Status: active "
    "| Contact: Charles Button | Assignees: Alina | Channel: Gmail "
    "| Next action: discuss syndication (2026-01-06) | Last update: Interested. "
    "| Source: Followers / Outreach\n\n"
    "2. Samsung NEXT Ventures | ID: abc | Type: corporate_venture | Status: archived "
    "| Source: Followers / Outreach, Followers / Outreach, List_Series_A / Pipeline_short\n\n"
    "3. Ki One | ID: xyz | Type: family_office | Status: active | Industry: robotics "
    "| Source: List_Series_A / FO"
)
_SAMPLE = json.dumps([{"response": _SAMPLE_INNER}])


def test_parse_dump_extracts_lean_fields() -> None:
    ents = r.parse_fr_dump(_SAMPLE)
    assert [e.name for e in ents] == ["Charles Button", "Samsung NEXT Ventures", "Ki One"]
    cb, sn, ko = ents
    assert cb.status == "active" and cb.entity_type == ""        # no Type on row 1
    assert sn.entity_type == "corporate_venture" and sn.status == "archived"
    assert ko.industry == "robotics" and ko.entity_type == "family_office"
    assert ko.fr_id == "xyz"


def test_parse_dump_dedups_sources() -> None:
    sn = r.parse_fr_dump(_SAMPLE)[1]
    # three raw entries, two distinct → deduped, order preserved
    assert sn.sources == ["Followers / Outreach", "List_Series_A / Pipeline_short"]


def test_lean_line_and_catalog_text() -> None:
    ents = r.parse_fr_dump(_SAMPLE)
    assert ents[2].lean_line() == "Ki One | family_office | active | robotics | List_Series_A / FO"
    txt = r.lean_catalog_text(ents)
    assert txt.count("\n") == 2 and "Charles Button | active | Followers / Outreach" in txt


def test_parse_dump_handles_bare_and_empty() -> None:
    assert r.parse_fr_dump("") == []
    # bare (unwrapped) text still parses
    assert [e.name for e in r.parse_fr_dump(_SAMPLE_INNER)] == [
        "Charles Button", "Samsung NEXT Ventures", "Ki One"]


def test_fetch_catalog_caches_within_ttl() -> None:
    r.reset_cache_for_tests()
    calls = {"n": 0}

    def fake_call_tool(**kw):
        calls["n"] += 1
        assert kw["tool_name"] == "humanoid_fr_search"
        assert kw["arguments"]["query"] == ""
        return True, _SAMPLE

    clock = {"t": 1000.0}
    out1 = r.fetch_catalog(mcp_url="u", ttl_seconds=100,
                           call_tool=fake_call_tool, now=lambda: clock["t"])
    out2 = r.fetch_catalog(mcp_url="u", ttl_seconds=100,
                           call_tool=fake_call_tool, now=lambda: clock["t"] + 50)
    assert len(out1) == 3 and len(out2) == 3
    assert calls["n"] == 1                       # second call served from cache
    # past TTL → refetch
    r.fetch_catalog(mcp_url="u", ttl_seconds=100,
                    call_tool=fake_call_tool, now=lambda: clock["t"] + 200)
    assert calls["n"] == 2


def test_fetch_catalog_returns_stale_on_failure() -> None:
    r.reset_cache_for_tests()
    state = {"ok": True}

    def flaky(**kw):
        return (True, _SAMPLE) if state["ok"] else (False, "boom")

    good = r.fetch_catalog(mcp_url="u2", ttl_seconds=0, call_tool=flaky, now=lambda: 0.0)
    assert len(good) == 3
    state["ok"] = False
    # ttl 0 forces refetch; failure → stale cache, not empty
    stale = r.fetch_catalog(mcp_url="u2", ttl_seconds=0, call_tool=flaky, now=lambda: 1.0)
    assert len(stale) == 3


def test_build_messages_separates_catalog_from_transcript() -> None:
    msgs = r.build_resolution_messages(
        meeting_title="Fundraising daily",
        participants=["Irina", "Alina"],
        transcript_or_summary="Обсудили Киван и Самсунг.",
        catalog_text="Ki One | family_office | active",
    )
    assert msgs[0]["role"] == "system"
    assert "Ki One" in msgs[1]["content"]                 # catalog is its own msg
    assert "Fundraising daily" in msgs[2]["content"]
    assert "Киван" in msgs[2]["content"]


def test_build_messages_truncates_long_transcript() -> None:
    msgs = r.build_resolution_messages(
        meeting_title="x", participants=None,
        transcript_or_summary="a" * 50000, catalog_text="c",
        max_transcript_chars=1000,
    )
    assert msgs[2]["content"].count("a") <= 1000


def test_parse_resolution_strict_and_fenced() -> None:
    out = r.parse_resolution(
        '```json\n[{"mention":"Киван","canonical":"Ki One","source":"FO","confidence":0.9},'
        '{"mention":"x","canonical":null,"confidence":0.2}]\n```'
    )
    assert len(out) == 2
    assert out[0].canonical == "Ki One" and out[0].confidence == 0.9
    assert out[1].canonical is None


def test_parse_resolution_grabs_array_amid_prose_and_skips_junk() -> None:
    out = r.parse_resolution('Here you go: [{"mention":"A","canonical":"Alpha","confidence":1}] thanks')
    assert len(out) == 1 and out[0].mention == "A"
    assert r.parse_resolution("not json at all") == []
    assert r.parse_resolution("") == []


def test_should_apply_gates() -> None:
    hi = r.Decision(mention="Киван", canonical="Ki One", confidence=0.9)
    lo = r.Decision(mention="Киван", canonical="Ki One", confidence=0.5)
    none = r.Decision(mention="x", canonical=None, confidence=0.99)
    assert r.should_apply(hi, current="Kivan") is True
    assert r.should_apply(lo, current="Kivan") is False          # below threshold
    assert r.should_apply(none, current=None) is False           # no canonical
    assert r.should_apply(hi, current="ki one") is False         # identity (case-insensitive)


__all__: list[str] = []
