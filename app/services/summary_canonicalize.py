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
from app.services.counterparty_match import (
    canonicalize_text,
    resolve_mentions_to_directory,
)

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
    a confident match.

    Match strategies (FR-CR-05-191 v3 — stricter than v2 to avoid
    false rewrites like «Chris Watkins» → «Chris Windle» (different
    people sharing first name) or «Кристиан Вольман» → «Кристиан»
    (mention longer than canonical, would lose surname):

      1. Exact case-insensitive match → already canonical, skip
      2. Single-token mention (e.g., «Boris»):
         - Must equal canonical's FIRST WORD
         - If multiple members share that first word → ambiguous, skip
      3. Multi-token mention (e.g., «Jared Cannon»):
         - LAST TOKEN of mention MUST appear in canonical
           (surname-based matching — first names may have typos but
           the family name anchors the identity)
         - Avoids same-first-name-different-surname false matches
           («Chris Doran» last="doran" not in «Chris Windle» tokens)
         - Avoids dropping surnames («Кристиан Вольман» last="вольман"
           not in «Кристиан» tokens)
         - Allows first-name typos to canonicalize («Jared Cannon»
           last="cannon" matches «Jarad Cannon» tokens ✓)
         - If multiple members tie for top shared-token score →
           ambiguous, skip
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
        mention_words = mention_norm.split()
        if not mention_words:
            continue
        # 1) Exact match — already canonical
        if any(_norm(m.real_name) == mention_norm for m in members):
            continue
        mention_word_count = len(mention_words)
        mention_last = mention_words[-1]
        # 2/3) Score every candidate
        candidates: list[tuple[TeamMember, int]] = []
        for m in members:
            canonical_norm = _norm(m.real_name)
            canonical_words = canonical_norm.split()
            if not canonical_words:
                continue
            if mention_word_count == 1:
                # Single-token mention must equal canonical's FIRST word
                if mention_words[0] != canonical_words[0]:
                    continue
                score = 1
            else:
                # Multi-token mention: last token (surname) must appear
                # in canonical's tokens. Avoids same-first-name matches.
                if mention_last not in set(canonical_words):
                    continue
                # Score by total token overlap so ties favour fuller match
                shared = set(mention_words) & set(canonical_words)
                score = len(shared)
            candidates.append((m, score))
        if not candidates:
            continue
        candidates.sort(key=lambda x: -x[1])
        top_score = candidates[0][1]
        top_candidates = [c for c in candidates if c[1] == top_score]
        # Ambiguous — more than one TeamMember tied at top → skip
        if len(top_candidates) > 1:
            continue
        best_member = top_candidates[0][0]
        canonical = best_member.real_name
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

    FR-CR-05-191c — hard-skip own-company / self-references like
    «Humanoid» so the LLM resolver doesn't accidentally rewrite
    them to phonetically-similar entries in the directory
    («Humane» is a different company; «Humanoid» is the operator's
    own company name and must NEVER be canonicalized).
    """
    if not mentions:
        return {}
    # Own-company name list — never rewrite these to anything.
    SELF_REFS = {"humanoid", "humain", "humanoid headquarters"}
    filtered = [
        m for m in mentions
        if (m or "").strip().lower() not in SELF_REFS
    ]
    if not filtered:
        return {}
    cps = session.query(Counterparty).all()
    if not cps:
        return {}
    # FR-CR-05-241 follow-up 2026-06-03 — when the canonical resolver is the
    # vector+critic catalog (mode=on), use it HERE too instead of the
    # directory-LLM. The directory-LLM dumps the whole directory into a prompt
    # with no vector grounding / confidence floor and force-matched phonetic
    # garbage in the summary text (Klef→Ross Cliff, K Stix→Styx on «Алина,
    # Ирина» 2026-06-03). The catalog resolver is recall-conservative and
    # critic-gated. off/shadow keep the legacy directory-LLM. Both return
    # {mention: counterparty_id | None}, so downstream is unchanged.
    from app.config import get_settings

    settings = get_settings()
    if getattr(settings, "counterparty_match_v2_mode", "off") == "on":
        from app.services.counterparty_catalog_resolver import (
            resolve_mentions_to_directory_via_catalog,
        )
        # transcript context is unavailable at canonicalisation time; the
        # critic falls back to mention-only context (still gated on score).
        resolved = resolve_mentions_to_directory_via_catalog(
            session, settings=settings, mentions=filtered,
            transcript="", directory=cps,
        )
    else:
        # LLM returns {mention: counterparty_id | None}
        resolved = resolve_mentions_to_directory(
            mentions=filtered,
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


def _seed_counterparties_from_orgs(
    orgs: list[str], session: Session,
) -> list[str]:
    """FR-CR-05-191b — auto-populate Counterparty directory from
    extracted org mentions. Operator-pinned 2026-05-21: «и из
    остальных ты всегда будешь так делать».

    For each org mention that survives the generic-filter, find or
    create a Counterparty row keyed by `name_normalised`. Returns
    the list of newly-created canonical names so the caller can log
    / trace what got added.
    """
    if not orgs:
        return []
    try:
        from app.sync.counterparties import normalise_name
    except Exception:  # noqa: BLE001
        return []
    # FR-CR-05-230 — auto-enroll kill-switch (default off): never mint
    # new counterparty cards from summary orgs; the directory changes
    # only via the Google-Sheet sync.
    from app.config import get_settings

    if not get_settings().counterparty_autoenroll_enabled:
        return []
    # Same generic filter as ops/seed_counterparties_from_summaries.py
    GENERIC = {
        "humanoid", "humain", "company", "fund", "investor",
        "investors", "bank", "banks", "government", "ventures",
        "capital", "partners", "advisors", "team", "office",
    }
    added: list[str] = []
    # Pre-fetch existing norms to avoid one query per org
    existing_norms = {
        n for (n,) in session.query(Counterparty.name_normalised).all()
    }
    for raw in orgs:
        if not raw or len(raw.strip()) < 3:
            continue
        norm = normalise_name(raw) or ""
        if not norm or norm in GENERIC or norm in existing_norms:
            continue
        cp = Counterparty(name=raw.strip(), name_normalised=norm)
        session.add(cp)
        try:
            session.flush()
        except Exception:  # noqa: BLE001 — race / dup
            session.rollback()
            continue
        existing_norms.add(norm)
        added.append(raw.strip())
    return added


def canonicalize_summary_text(
    text: str | None,
    *,
    session: Session,
    llm_backend: LLMBackend,
    model: str,
    trace_source: str = "summary",
    trace_recording_id: str | None = None,
    auto_seed_counterparties: bool = True,
) -> tuple[str | None, dict[str, str]]:
    """End-to-end: extract entities → optionally auto-seed
    Counterparty (FR-CR-05-191b) → resolve people + orgs →
    canonicalize_text.

    Returns ``(new_text, applied_rewrites)``. ``applied_rewrites``
    is empty when nothing changed.
    """
    if not text or not text.strip():
        return text, {}
    entities = extract_name_entities(
        text, llm_backend=llm_backend, model=model,
    )
    if auto_seed_counterparties:
        seeded = _seed_counterparties_from_orgs(
            entities["organizations"], session,
        )
        if seeded:
            log.info(
                "summary_canonicalize_counterparty_seeded",
                source=trace_source, recording_id=trace_recording_id,
                added=len(seeded), examples=seeded[:5],
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
