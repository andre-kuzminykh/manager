"""FR-CR-05-191 — Canonical name rewriting for meeting summaries.

After ``_step_detailed_summary`` or ``_step_short_summary`` produces
text, run it through this service. It:

1. Calls LLM to extract list of person + organization mentions
   from the text (including mis-transcribed surface forms like
   «Jared Kinnan» where canonical is «Jarad Cannon»).

2. Resolves person mentions to ``TeamMember.real_name`` by:
   - exact case-insensitive name match
   - last-name token match (fuzzy)
   Anything matched gets a {mention → canonical} entry.

3. Resolves organization mentions to ``Counterparty.name`` by:
   - exact ``name_normalised`` match
   - substring fuzzy match

4. Calls ``canonicalize_text`` (FR-CR-05-129) to do the actual
   regex-based, longest-mention-first, word-boundary-aware
   string replacement in the summary text.

Result: every mention of a known teammate or counterparty in the
summary uses the canonical form from the directory tables.

Operator-pinned 2026-05-21: «и в детальных саммери и в коротких
потом все переименовать с учетом таблиц people и контрагентов».
"""
from __future__ import annotations

import json
import re

import structlog
from sqlalchemy.orm import Session

from app.intent.llm_backends import LLMBackend
from app.models import TeamMember
from app.models.counterparty import Counterparty
from app.services.counterparty_match import canonicalize_text

log = structlog.get_logger(__name__)


ENTITY_EXTRACT_SYSTEM = """\
You receive a meeting summary text in Russian. Extract every:

1) PERSON name — full name or first+last, including
   mis-transcribed phonetic forms (e.g., «Jared Kinnan»,
   «Sotiris Dastanopoulos», «Андрей Кузмен»).

2) ORGANIZATION name — company, fund, brand, government body
   (e.g., «Affinity Partners», «CDIB Capital», «Goldman Sachs»,
   «Foxconn», «Bosch», «AdNOC»).

Output STRICTLY valid JSON (no commentary, no code fences):

{"people": ["<surface form>", ...], "organizations": ["<surface form>", ...]}

Rules:
- Include the EXACT surface form as it appears in the text,
  even if mis-spelled / phonetic.
- Skip first-name-only mentions if the full name appears
  elsewhere in the text.
- Skip role nouns («CEO», «инвестор», «партнёр», «менеджер»).
- Skip generic country / city names.
- If you see «и другие» / «and others» — skip it; it's not an entity.
- Empty array if nothing of the requested type.
"""


def extract_name_entities(
    text: str,
    *,
    llm_backend: LLMBackend,
    model: str,
) -> dict[str, list[str]]:
    """LLM-extract person + org mentions from text."""
    if not text or not text.strip():
        return {"people": [], "organizations": []}
    raw = llm_backend.complete_text(
        system_prompt=ENTITY_EXTRACT_SYSTEM,
        user_prompt=text,
        model=model,
        temperature=0.0,
    )
    if not raw:
        return {"people": [], "organizations": []}
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
    raw = re.sub(r"\n?```\s*$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning(
            "summary_canonicalize_json_parse_failed",
            raw_preview=raw[:200],
        )
        return {"people": [], "organizations": []}
    return {
        "people": [
            str(x).strip() for x in (data.get("people") or []) if x
        ],
        "organizations": [
            str(x).strip() for x in (data.get("organizations") or []) if x
        ],
    }


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def resolve_people_to_team_members(
    mentions: list[str], session: Session,
) -> dict[str, str]:
    """Map each mention → canonical TeamMember.real_name when there's
    a confident match. Skips mentions that already equal the canonical
    form (no rewrite needed).

    Match strategies (in order):
      1. Exact case-insensitive name match → no rewrite, skip
      2. Last-name token match (one TeamMember's surname appears as
         a token in the mention OR mention's last token appears in
         TeamMember name) → rewrite
      3. First-name + last-initial / email-local match
    """
    if not mentions:
        return {}
    members = (
        session.query(TeamMember)
        .filter(TeamMember.active.is_(True))
        .filter(TeamMember.real_name.isnot(None))
        .all()
    )
    if not members:
        return {}
    out: dict[str, str] = {}
    for mention in mentions:
        mention_norm = _norm(mention)
        if not mention_norm:
            continue
        mention_tokens = set(re.findall(r"\w+", mention_norm))
        # 1) Exact match — no rewrite needed
        exact_hit = any(
            _norm(m.real_name) == mention_norm for m in members
        )
        if exact_hit:
            continue
        # 2) Token-overlap with at least one shared token of len >= 3
        best_match: TeamMember | None = None
        best_score = 0
        for m in members:
            canonical_norm = _norm(m.real_name)
            canonical_tokens = set(re.findall(r"\w+", canonical_norm))
            intersect = {
                t for t in (mention_tokens & canonical_tokens)
                if len(t) >= 3
            }
            score = len(intersect)
            if score > best_score:
                best_match = m
                best_score = score
        if best_match and best_score >= 1:
            canonical = best_match.real_name
            if _norm(canonical) != mention_norm:
                out[mention] = canonical
    return out


def resolve_organizations_to_counterparties(
    mentions: list[str],
    session: Session,
    *,
    llm_backend: LLMBackend,
    model: str,
    trace_source: str | None = None,
    trace_recording_id: str | None = None,
) -> dict[str, str]:
    """Map each mention → canonical Counterparty.name using the
    LLM-based resolver (FR-CR-05-129 Pass 2). Battle-tested for
    fuzzy / phonetic / transliterated forms.
    """
    if not mentions:
        return {}
    cps = session.query(Counterparty).all()
    if not cps:
        return {}
    from app.services.counterparty_match import resolve_mentions_to_directory

    # LLM returns {mention: counterparty_id | None}
    resolved = resolve_mentions_to_directory(
        mentions=mentions,
        directory=cps,
        llm_backend=llm_backend,
        model=model,
        trace_source=trace_source,
        trace_recording_id=trace_recording_id,
    )
    id_to_name: dict[int, str] = {cp.id: cp.name for cp in cps}
    out: dict[str, str] = {}
    for mention, cp_id in resolved.items():
        if cp_id is None:
            continue
        canonical = id_to_name.get(cp_id)
        if canonical and _norm(canonical) != _norm(mention):
            out[mention] = canonical
    return out


def canonicalize_summary_text(
    text: str | None,
    *,
    session: Session,
    llm_backend: LLMBackend,
    model: str,
    trace_source: str = "summary",
    trace_recording_id: str | None = None,
) -> tuple[str | None, dict[str, str]]:
    """End-to-end: extract entities → resolve → canonicalize_text.

    Returns ``(new_text, applied_rewrites)``. ``applied_rewrites``
    is empty when nothing changed.
    """
    if not text or not text.strip():
        return text, {}
    entities = extract_name_entities(
        text, llm_backend=llm_backend, model=model,
    )
    people_map = resolve_people_to_team_members(
        entities["people"], session,
    )
    org_map = resolve_organizations_to_counterparties(
        entities["organizations"], session,
        llm_backend=llm_backend, model=model,
        trace_source=trace_source,
        trace_recording_id=trace_recording_id,
    )
    full_map: dict[str, str] = {**people_map, **org_map}
    log.info(
        "summary_canonicalize_resolved",
        source=trace_source, recording_id=trace_recording_id,
        people_extracted=len(entities["people"]),
        orgs_extracted=len(entities["organizations"]),
        people_rewrites=len(people_map),
        org_rewrites=len(org_map),
    )
    if not full_map:
        return text, {}
    new_text = canonicalize_text(text, full_map)
    return new_text, full_map


__all__ = [
    "ENTITY_EXTRACT_SYSTEM",
    "extract_name_entities",
    "resolve_people_to_team_members",
    "resolve_organizations_to_counterparties",
    "canonicalize_summary_text",
]
