"""FR-EC-CRITIC Track 2 — agentic people resolver. All LLM/MCP injected.
Models the «Самир» → «Samer Zawadeih» (in Tawazun's comm_log) flow.
"""
from __future__ import annotations

from app.services.entity_people_fr import (
    build_person_extract_messages,
    build_person_org_messages,
    resolve_people,
)
from app.services.entity_resolver_fr import Decision

_TAWAZUN = (
    "1. Tawazun Strategic Development Fund | Type: sovereign_fund | "
    "Next action: Zia meeting with Samer from SDF | Communication log:\n"
    "02/04 I had a productive call with Samer Zawadeih and Muaatasem Awda."
)


def test_org_messages_carry_resolved_hints_and_context() -> None:
    msgs = build_person_org_messages(
        transcript="Обсудили Самир из SDF", meeting_title="Fundraising",
        participants=["Alina"], already_resolved=["Tether", "Key 1 Capital"],
        org_hints=["Tawazun Strategic Development Fund", "Jabal"])
    assert "Tether" in msgs[1]["content"]                 # already-resolved skip-list
    assert "Jabal" in msgs[2]["content"]                  # org hints
    assert "Fundraising" in msgs[3]["content"] and "SDF" in msgs[3]["content"]


def test_extract_messages_embed_records_and_pairs() -> None:
    msgs = build_person_extract_messages(
        person_orgs=[("Самир", "Tawazun")], org_records=[("Tawazun", _TAWAZUN)],
        meeting_title="m", transcript="t")
    assert "Samer Zawadeih" in msgs[1]["content"]       # record prose present
    assert "Самир -> org: Tawazun" in msgs[2]["content"]


def test_resolve_people_full_flow() -> None:
    seen = {}

    def call_orgs(_msgs):
        return [("Самир", "Tawazun"), ("DEV", "")]          # DEV has no org → dropped

    def search_fn(org):
        seen["searched"] = org
        return _TAWAZUN

    def call_extract(_msgs):
        return [Decision(mention="Самир", canonical="Samer Zawadeih",
                         source="Tawazun", confidence=0.9)]

    out = resolve_people(
        transcript="Обсудили Самир и DEV", meeting_title="m",
        participants=None, already_resolved=[],
        call_orgs=call_orgs, search_fn=search_fn, call_extract=call_extract)
    assert seen["searched"] == "Tawazun"
    assert len(out) == 1 and out[0].canonical == "Samer Zawadeih"


def test_resolve_people_no_orgs_short_circuits() -> None:
    calls = {"search": 0}

    def call_orgs(_msgs):
        return [("DEV", "")]                                # nothing with an org

    def search_fn(org):
        calls["search"] += 1
        return "x"

    out = resolve_people(transcript="DEV stuff", meeting_title="m",
                         participants=None, already_resolved=[],
                         call_orgs=call_orgs, search_fn=lambda o: (_ for _ in ()).throw(AssertionError),
                         call_extract=lambda m: [])
    assert out == []


def test_resolve_people_empty_input() -> None:
    assert resolve_people(transcript="   ", meeting_title="m", participants=None,
                          already_resolved=[], call_orgs=lambda m: [],
                          search_fn=lambda o: "", call_extract=lambda m: []) == []


def test_resolve_people_swallows_failures() -> None:
    def boom(_msgs):
        raise RuntimeError("llm down")
    out = resolve_people(transcript="Самир", meeting_title="m",
                         participants=None, already_resolved=[],
                         call_orgs=boom, search_fn=lambda o: "x", call_extract=lambda m: [])
    assert out == []


# -- integration with the step: people-track merges + records kind=person -----

def test_step_runs_people_track_when_enabled() -> None:
    from dataclasses import dataclass

    from app.services import entity_resolver_fr as R
    from app.services.fr_resolve_step import resolve_for_meeting

    @dataclass
    class S:
        entity_fr_resolver_enabled: bool = True
        entity_fr_resolver_shadow: bool = False
        entity_fr_mcp_url: str = "u"
        entity_fr_catalog_ttl_seconds: int = 3600
        entity_fr_max_context_tokens: int = 30000
        entity_fr_shard_workers: int = 2
        entity_fr_min_confidence: float = 0.7
        entity_fr_resolver_model: str = "gpt-5.5"
        entity_fr_people_enabled: bool = True
        entity_fr_people_max_orgs: int = 6

    cat = [R.FrEntity(name="Tether", sources=["Followers"])]
    rec = []

    # main map returns one company + one UNRESOLVED person; the SAME callable is
    # reused as the people-extract step — branch on the extract system prompt.
    def call(messages):
        if "FULL NAME" in messages[0]["content"]:        # Track-2 extract step
            return [R.Decision(mention="Самир", canonical="Samer Zawadeih",
                               source="Tawazun", confidence=0.9)]
        return [R.Decision(mention="Tether", canonical="Tether", confidence=0.95),
                R.Decision(mention="Самир", canonical=None, confidence=0.2)]

    out = resolve_for_meeting(
        settings=S(), text="Обсудили Tether и Самир из SDF", meeting_title="m",
        participants=None, source="zoom", source_id="z1",
        catalog=cat, team_rows=[], call=call,
        people_call_orgs=lambda m: [("Самир", "Tawazun")],
        people_search_fn=lambda org: _TAWAZUN,
        recorder=lambda d, **k: rec.append((d.mention, k["kind"], d.canonical, k["applied"])),
    )
    assert out.get("Самир") == "Samer Zawadeih"  # Track-2 person merged into replacements
    kinds = {m: k for m, k, *_ in rec}
    assert kinds.get("Самир") == "person"        # people-track tagged kind=person
    assert kinds.get("Tether") == "company"


__all__: list[str] = []
