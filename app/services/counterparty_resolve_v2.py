"""FR-CR-05-241 — v2 counterparty resolution (pgvector top-K → critic),
the building block for replacing the slow whole-directory LLM resolve in
the Zoom/Fireflies pipeline. PURE orchestration: no DB writes, no Slack,
no enrollment side-effects — so it's safe to compute in SHADOW alongside
v1 before any cutover.

WHY (operator 2026-06-01): the live `match_counterparties` step takes
~8.85 min/recording (whole directory stuffed into the LLM) AND auto-
enrolls every unresolved mention as a new counterparty — which polluted
the prod directory with speech-to-text garbage («Забалты», «Не бучи»,
«День Z», «сугу»…). v2 fixes both: per-mention pgvector retrieval +
tightened critic (seconds, precise) and a CONSERVATIVE ENROLLMENT POLICY.

ENROLLMENT POLICY (the precision-critical contract):
  Under v2 an unresolved mention (critic → matched_entity_id is None) is
  NEVER auto-enrolled into `counterparties`. It is surfaced as
  `unresolved` for review/logging only. Enrollment of genuinely-new real
  entities is an explicit, separate, curated action — not a side-effect
  of a garbled transcript. (v1 enrolled ALL unresolved → the pollution.)

SHADOW CONTRACT:
  In shadow mode the pipeline keeps v1 as the source of truth and only
  LOGS `shadow_diff(v1, v2)` — agreements, v1-only, v2-only, both-none —
  with timings. No behaviour change → zero risk.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.logging_setup import get_logger
from app.models.entity_embedding import KIND_COUNTERPARTY
from app.services.entity_match_v2 import DEFAULT_K, match_entity

log = get_logger(__name__)

# retrieve_fn(query_text, k) -> [{entity_id, text_repr, score}]
RetrieveFn = Callable[[str, int], list[dict[str, Any]]]


@dataclass
class V2Resolution:
    mention: str
    matched_entity_id: int | None
    confidence: float
    reasoning: str


def resolve_mentions_v2(
    *,
    mentions: list[str],
    retrieve_fn: RetrieveFn,
    backend: Any,
    context_for: Callable[[str], str] | None = None,
    critic_model: str | None = None,
    k: int = DEFAULT_K,
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
    "V2Resolution", "resolve_mentions_v2", "partition",
    "mentions_to_enroll", "shadow_diff",
]
