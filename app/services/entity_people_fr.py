"""FR-EC-CRITIC Track 2 — agentic resolver for counterparty PEOPLE.

External contacts (e.g. «Самир» → Samer Zawadeih) are NOT company rows; their
full names live in the prose fields (next_action / last_update /
communication_log) of a company record. Like Viktor's chat agent we resolve
them in a targeted second pass, NOT by dumping every comm_log:

    unresolved person-like mentions
      └─ LLM #1: is it a person? which org/fund? (meeting context)  → [(mention, org)]
      └─ humanoid_fr_search(org) for each distinct org (≤ max_orgs) → full records
      └─ LLM #2: extract canonical full name from the records' prose → [Decision]

All LLM/MCP access is injected (call_orgs / call_extract / search_fn) so this
unit-tests with zero network. Returns [] on anything missing — best-effort,
never raises (the caller's Track 1/3 results are unaffected).
"""
from __future__ import annotations

from typing import Callable

from app.logging_setup import get_logger
from app.services.entity_resolver_fr import Decision

log = get_logger(__name__)

# Tool schema for LLM #1 (person? + org guess).
PERSON_ORG_TOOL = {
    "type": "object",
    "properties": {
        "people": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "mention": {"type": "string"},
                    "is_person": {"type": "boolean"},
                    "org": {"type": "string",
                            "description": "org/fund the person is associated with, from meeting context; empty if unknown"},
                },
                "required": ["mention", "is_person", "org"],
            },
        }
    },
    "required": ["people"],
}

_ORG_SYSTEM = (
    "From a meeting transcript, decide which UNRESOLVED mentions are PEOPLE "
    "(external contacts, not companies) and, for each, name the org/fund they "
    "are associated with based ONLY on the meeting context (e.g. «Самир» is the "
    "SDF / Tawazun contact → org «Tawazun»). Set is_person=false for "
    "companies/products. Leave org empty if the context gives no org. STRICT "
    "JSON via the tool only."
)

_EXTRACT_SYSTEM = (
    "You are given CRM records (including their communication-log prose) for a "
    "few orgs, plus person mentions from a meeting and the org each belongs to. "
    "For each person mention, extract their CANONICAL FULL NAME exactly as it "
    "appears in the CRM prose (e.g. «Самир» → «Samer Zawadeih»). Use the org "
    "record as the source. If the full name is not present in the records, set "
    "canonical=null. Do NOT invent. Output STRICT JSON list of "
    '{"mention","canonical","source","confidence"} via the tool.'
)


def build_person_org_messages(
    *, unresolved_mentions: list[str], meeting_title: str | None,
    participants: list[str] | None, transcript: str, max_transcript_chars: int = 16000,
) -> list[dict[str, str]]:
    ctx = f"Meeting: {meeting_title or '(untitled)'}"
    if participants:
        ctx += "\nParticipants: " + ", ".join(participants)
    return [
        {"role": "system", "content": _ORG_SYSTEM},
        {"role": "user", "content": "UNRESOLVED MENTIONS:\n" + "\n".join(unresolved_mentions)},
        {"role": "user", "content": ctx + "\n\nTRANSCRIPT/SUMMARY:\n"
                                    + (transcript or "")[:max_transcript_chars]},
    ]


def build_person_extract_messages(
    *, person_orgs: list[tuple[str, str]], org_records: list[tuple[str, str]],
    meeting_title: str | None, transcript: str, max_record_chars: int = 6000,
    max_transcript_chars: int = 8000,
) -> list[dict[str, str]]:
    recs = "\n\n".join(f"### ORG: {org}\n{rec[:max_record_chars]}" for org, rec in org_records)
    pairs = "\n".join(f"{m} -> org: {o}" for m, o in person_orgs)
    return [
        {"role": "system", "content": _EXTRACT_SYSTEM},
        {"role": "user", "content": "CRM RECORDS:\n" + recs},
        {"role": "user", "content": "PERSON MENTIONS:\n" + pairs
                                    + "\n\nMeeting: " + (meeting_title or "(untitled)")
                                    + "\nTRANSCRIPT/SUMMARY:\n" + (transcript or "")[:max_transcript_chars]},
    ]


def resolve_people(
    *,
    unresolved_mentions: list[str],
    meeting_title: str | None,
    participants: list[str] | None,
    transcript: str,
    call_orgs: Callable[[list[dict]], list[tuple[str, str]]],
    search_fn: Callable[[str], str],
    call_extract: Callable[[list[dict]], list[Decision]],
    max_orgs: int = 6,
) -> list[Decision]:
    """Agentic Track-2 resolve. Injected: `call_orgs(messages)->[(mention,org)]`,
    `search_fn(org)->verbose record text`, `call_extract(messages)->[Decision]`."""
    if not unresolved_mentions:
        return []
    try:
        person_orgs = call_orgs(build_person_org_messages(
            unresolved_mentions=unresolved_mentions, meeting_title=meeting_title,
            participants=participants, transcript=transcript)) or []
    except Exception as e:  # noqa: BLE001
        log.info("fr_people_org_step_failed", error=str(e))
        return []
    person_orgs = [(m, o) for m, o in person_orgs if m and o]
    if not person_orgs:
        return []

    # distinct orgs, capped
    orgs: list[str] = []
    for _m, o in person_orgs:
        if o not in orgs:
            orgs.append(o)
    orgs = orgs[:max_orgs]

    records: list[tuple[str, str]] = []
    for org in orgs:
        try:
            rec = search_fn(org)
        except Exception as e:  # noqa: BLE001
            log.info("fr_people_search_failed", org=org, error=str(e))
            continue
        if rec and rec.strip():
            records.append((org, rec))
    if not records:
        return []

    try:
        decisions = call_extract(build_person_extract_messages(
            person_orgs=person_orgs, org_records=records,
            meeting_title=meeting_title, transcript=transcript)) or []
    except Exception as e:  # noqa: BLE001
        log.info("fr_people_extract_step_failed", error=str(e))
        return []
    return [d for d in decisions if d.canonical]


__all__ = [
    "PERSON_ORG_TOOL",
    "build_person_org_messages",
    "build_person_extract_messages",
    "resolve_people",
]
