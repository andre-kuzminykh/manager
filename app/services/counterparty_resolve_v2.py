"""FR-CR-05-241 — entity resolution for the **4000-entity catalog**
(operator 2026-06-01: «мы делаем для 4000»). TARGET ARCHITECTURE = pure
RAG, because a directory of ~4000 names CANNOT be stuffed into an LLM
prompt:

    transcript
      → extract (gpt-5.5, ONE call) → mentions
      → per mention:
           pgvector top-K (k≈20, widen on low confidence)   # NOT the whole 4000
           critic gpt-4o over those K candidates only
           ├─ matched → link to a catalog entity
           └─ none    → REVIEW QUEUE (never auto-enrolled, never lost)

WHY: the legacy `match_counterparties` step stuffed the WHOLE directory
into the LLM (~8.85 min) AND auto-enrolled every unresolved mention as a
new counterparty — polluting the directory with speech-to-text garbage
(«Забалты», «Не бучи», «сугу», «Long Ball Finance»…). At 4000 names the
whole-dir trick is impossible anyway; RAG + a conservative enrollment
policy is the only thing that scales AND stays clean.

ENROLLMENT POLICY (precision-critical):
  An unresolved mention (critic → None, even after widening k) is NEVER
  auto-enrolled. It goes to a REVIEW QUEUE (`review_queue(...)`) for the
  operator to curate — genuinely-new entities are added via the source
  export + rebuild, not as a side-effect of a garbled transcript.

RECALL levers at 4000 scale (no «whole base in context»):
  1. larger k (top-50/100 = 100 names in the prompt, not 4000);
  2. retrieval quality — clean catalog + acronym/Cyrillic aliases;
  3. extract quality (gpt-5.5 + name-bias → cleaner surface forms).
  If still none → review queue. That is the ceiling, moved by k/aliases/
  extract — NOT by dumping the directory into the LLM.

NOTE: `resolve_mentions_hybrid` (below) keeps a whole-directory v1
FALLBACK — that is **847-only legacy** (a small directory fits a prompt).
It does NOT apply to the 4000 catalog target and is kept only for the
small-directory scenario.

VALIDATED ON DIRTY DATA (operator run 2026-06-01, prod transcript
"Fundraising daily", gpt-5.5 extract + gpt-4o critic + k=20 + transcript
context):
  * precision is the win — 0 false positives. The old whole-dir critic
    force-matched garbled forms (Винроботикс→Rainbow, Маслон→Mistral,
    сугу→Genia, День Z→2PZ); v2 correctly returns NONE for all of them.
  * legitimate garbled same-name matches are recovered BY CONTEXT:
    Xtix→XTX Markets, Митсобиш(+электроникс)→Mitsubishi, ДВНЕ→DN Capital,
    Люната→Lunate, Блюму→Blume, Севе/Севы→CEVA — these miss WITHOUT
    context (the retrieval query / critic prompt carry ≤300 / ≤1500 chars
    of surrounding transcript, see entity_match_v2._query_text /
    build_critic_user_prompt). Context is the recall lever, NOT a looser
    critic prompt.
  * recall is now bounded by CATALOG MEMBERSHIP, not the critic: real
    entities absent from the 4000 (SpaceX, Cargill, Anthropic…) correctly
    go to the review queue for curation, exactly as designed.
  * extract MUST be gpt-5.5 reasoning — gpt-4o w/o reasoning is too shallow
    for ASR-garbled forms (it surfaced 11 clean names vs 41 for gpt-5.5).
    Cost note: extract is ONE call per RECORDING and is INDEPENDENT of the
    4000 catalog size (the catalog never enters the extract prompt; it is
    reached only via pgvector retrieval + ≤k critic candidates).

ROLLOUT (shadow-first, gated by COUNTERPARTY_MATCH_V2_MODE, default "off"):
  Phase 0  off    — v1 whole-dir resolver only (current prod).
  Phase 1  shadow — v1 stays canonical; `counterparty_shadow_v2.
                    shadow_compare_v2` ALSO resolves against the catalog and
                    LOGS the name-based diff (both/v1_only/v2_only). Zero
                    writes, never raises. Validate on live meetings.
  Phase 2  on     — v2 is the SINGLE canonical resolver (implemented in
                    counterparty_catalog_resolver.resolve_mentions_to_
                    directory_via_catalog, wired into both pipelines). A
                    matched catalog entity is mapped back to a `counterparties`
                    row by name_normalised (catalog ids ≠ counterparties ids).
                    CONSERVATIVE: links ONLY to entities already in the
                    directory; a real catalog entity absent from it → None
                    (review) and is NEVER auto-created — the directory's source
                    of truth is the Google Sheet, which would wipe an
                    auto-created row. Reversible instantly via the flag (→off).

PROD INFRA (operator decision 2026-06-01: SEPARATE pgvector instance — the
prod transactional DB is NEVER touched / migrated):
  1. run a dedicated pgvector/pgvector:pg16 container = the catalog DB (it
     can be the existing sidecar that already holds the 4030-entity catalog
     + embeddings); apply migrations 0037/0038 THERE, not on prod;
  2. load + embed the operator-curated catalog into it (kind='catalog') —
     ops.catalog_vector_search;
  3. set CATALOG_DATABASE_URL on the prod app to that container; the shadow
     hook / v2 read the catalog over this second connection
     (app.db.get_catalog_session_factory), leaving the prod DB alone;
  4. set COUNTERPARTY_MATCH_V2_MODE=shadow, watch `counterparty_shadow_v2`
     logs for a few days; only then consider Phase 2.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.logging_setup import get_logger
from app.models.entity_embedding import KIND_COUNTERPARTY
from app.services.entity_match_v2 import match_entity

log = get_logger(__name__)

# 4000-catalog default retrieval width (operator 2026-06-01: «дефолт k 12→20»).
# Wider than the 847-tuned DEFAULT_K so rare/garbled names still enter the
# critic's candidate window without the whole base in context.
CATALOG_DEFAULT_K = 20

# retrieve_fn(query_text, k) -> [{entity_id, text_repr, score}]
RetrieveFn = Callable[[str, int], list[dict[str, Any]]]


@dataclass
class V2Resolution:
    mention: str
    matched_entity_id: int | None
    confidence: float
    reasoning: str
    method: str = "v2"  # "v2" | "v1_fallback" | "none"


def resolve_mentions_v2(
    *,
    mentions: list[str],
    retrieve_fn: RetrieveFn,
    backend: Any,
    context_for: Callable[[str], str] | None = None,
    critic_model: str | None = None,
    k: int = CATALOG_DEFAULT_K,
) -> list[V2Resolution]:
    """Resolve each mention via pgvector top-K + critic. No side effects."""
    out: list[V2Resolution] = []
    for m in mentions:
        ctx = context_for(m) if context_for else m
        res = match_entity(
            kind=KIND_COUNTERPARTY, mention=m, context=ctx,
            retrieve_fn=retrieve_fn, backend=backend,
            critic_model=critic_model, k=k,
        )
        mid = res.get("matched_entity_id")
        mid_int = int(mid) if mid is not None and str(mid).isdigit() else None
        out.append(V2Resolution(
            mention=m, matched_entity_id=mid_int,
            confidence=float(res.get("confidence", 0.0)),
            reasoning=res.get("reasoning", "") or "",
        ))
    return out


def resolve_mentions_hybrid(
    *,
    mentions: list[str],
    retrieve_fn: RetrieveFn,
    backend: Any,
    v1_resolve_fn: Callable[[list[str]], dict[str, int | None]],
    context_for: Callable[[str], str] | None = None,
    critic_model: str | None = None,
    k: int = CATALOG_DEFAULT_K,
) -> list[V2Resolution]:
    """FR-CR-05-241 hybrid — recall-safe rollout of v2.

    Fast path: v2 (pgvector top-K + critic). For ONLY the mentions v2
    can't resolve (`none`), fall back to v1 (`v1_resolve_fn` = the whole-
    directory LLM resolver, which handles speech-to-text-garbled forms a
    top-K miss would drop, e.g. «хабспот»→HubSpot, «BofA»). So:
      * recall ≥ v1 (fallback covers every v2 miss),
      * cost ≈ v2 for the confident majority — v1 LLM runs ONLY on the
        small none-set, not on every mention,
      * enrollment policy UNCHANGED: a mention still `none` after BOTH
        stages stays unresolved and is NEVER auto-enrolled.

    `v1_resolve_fn(mentions) -> {mention: counterparty_id | None}` is
    injected so this stays pure/testable (no DB/LLM here)."""
    v2 = resolve_mentions_v2(
        mentions=mentions, retrieve_fn=retrieve_fn, backend=backend,
        context_for=context_for, critic_model=critic_model, k=k,
    )
    none_mentions = [r.mention for r in v2 if r.matched_entity_id is None]
    v1_map = v1_resolve_fn(none_mentions) if none_mentions else {}
    out: list[V2Resolution] = []
    for r in v2:
        if r.matched_entity_id is not None:
            r.method = r.method or "v2"
            out.append(r)
            continue
        mid = v1_map.get(r.mention)
        if mid is not None:
            out.append(V2Resolution(
                mention=r.mention, matched_entity_id=int(mid),
                confidence=r.confidence, reasoning="v1 whole-dir fallback",
                method="v1_fallback",
            ))
        else:
            r.method = "none"
            out.append(r)
    return out


def partition(resolutions: list[V2Resolution]) -> tuple[list[V2Resolution], list[str]]:
    """(matched, unresolved-surface-forms). Matched ones link to an
    existing counterparty; unresolved are NEVER enrolled (policy)."""
    matched = [r for r in resolutions if r.matched_entity_id is not None]
    unresolved = [r.mention for r in resolutions if r.matched_entity_id is None]
    return matched, unresolved


def mentions_to_enroll(resolutions: list[V2Resolution]) -> list[str]:
    """ENROLLMENT POLICY (FR-CR-05-241): v2 auto-enrolls NOTHING. Always
    returns []. Unresolved mentions are surfaced via `partition`, not
    written to the directory — this is the fix for the prod pollution."""
    return []


def review_queue(resolutions: list[V2Resolution]) -> list[dict[str, Any]]:
    """4000-target REVIEW QUEUE (replaces auto-enroll).

    An unresolved mention (critic → None even after widening k) is NOT
    written to the catalog — it is emitted here for the operator to curate.
    Genuinely-new entities are then added via the source export + rebuild,
    not as a side-effect of a garbled transcript surface form.

    Returns one row per unresolved mention:
      {surface_form, confidence, reasoning, method}
    Matched mentions are excluded (they already link to a catalog entity).
    De-duplicates repeated surface forms (keeps the highest-confidence
    near-miss so the operator sees the strongest signal). Writes nothing —
    the caller persists/surfaces these rows (table or log)."""
    best: dict[str, V2Resolution] = {}
    for r in resolutions:
        if r.matched_entity_id is not None:
            continue
        prev = best.get(r.mention)
        if prev is None or r.confidence > prev.confidence:
            best[r.mention] = r
    return [
        {
            "surface_form": r.mention,
            "confidence": r.confidence,
            "reasoning": r.reasoning,
            "method": r.method,
        }
        for r in best.values()
    ]


def shadow_diff(
    v1_matched_ids: dict[str, int | None],
    v2: list[V2Resolution],
) -> dict[str, Any]:
    """Structured comparison for shadow logging. `v1_matched_ids` maps a
    mention → the counterparty id v1 picked (or None). Returns counts +
    a small sample of disagreements; writes nothing."""
    agree = v1_only = v2_only = both_none = disagree_id = 0
    samples: list[dict[str, Any]] = []
    v2_by_m = {r.mention: r for r in v2}
    for m, v1id in v1_matched_ids.items():
        r = v2_by_m.get(m)
        v2id = r.matched_entity_id if r else None
        if v1id is None and v2id is None:
            both_none += 1
        elif v1id is not None and v2id is None:
            v1_only += 1
            if len(samples) < 25:
                samples.append({"mention": m, "v1": v1id, "v2": None})
        elif v1id is None and v2id is not None:
            v2_only += 1
            if len(samples) < 25:
                samples.append({"mention": m, "v1": None, "v2": v2id})
        elif v1id == v2id:
            agree += 1
        else:
            disagree_id += 1
            if len(samples) < 25:
                samples.append({"mention": m, "v1": v1id, "v2": v2id})
    return {
        "total": len(v1_matched_ids),
        "agree": agree, "disagree_id": disagree_id,
        "v1_only": v1_only, "v2_only": v2_only, "both_none": both_none,
        "samples": samples,
    }


__all__ = [
    "V2Resolution", "resolve_mentions_v2", "resolve_mentions_hybrid",
    "partition", "mentions_to_enroll", "review_queue", "shadow_diff",
    "CATALOG_DEFAULT_K",
]
