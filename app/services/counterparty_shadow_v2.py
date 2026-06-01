"""FR-CR-05-241 — SHADOW hook for vector counterparty resolution (v2).

Resolve a transcript's already-extracted counterparty mentions against the
NEW vector catalog (`entity_catalog_staging`, embeddings kind='catalog') and
LOG how v2 would differ from the v1 result the live pipeline just wrote.

SAFETY (this is the whole point of the module):
  * READ-ONLY — writes nothing to the DB.
  * NEVER raises — every failure is swallowed and logged, so the shadow can
    never break or slow-fail the canonical v1 pipeline.
  * v1 stays fully canonical; this only observes.

Why shadow-first: the v2 cutover needs prod-side infra that is NOT yet
deployed — pgvector in the prod DB, migrations 0037/0038, and the catalog +
embeddings load — and v2 resolves against a DIFFERENT id-space (catalog ids
≠ counterparties ids). Shadow validates v2 on real meetings at ZERO risk
before any of that. Gated by COUNTERPARTY_MATCH_V2_MODE (default "off").

Comparison is by NORMALISED NAME (v1 ids are prod-directory ids, v2 ids are
catalog ids — different directories), mirroring ops.shadow_report: we compare
which real entities each method surfaced, not raw ids.
"""
from __future__ import annotations

import time
from typing import Any, Iterable

from app.logging_setup import get_logger
from app.services.entity_catalog import (
    CATALOG_KIND,
    _normalise_name,
    build_catalog_lexical_index,
    context_window,
)
from app.services.entity_embeddings import make_openai_embed_fn, search_entities
from app.services.entity_match_v2 import match_entity

log = get_logger(__name__)


def shadow_compare_v2(
    session: Any,
    *,
    settings: Any,
    mentions: list[str],
    v1_canonical_names: Iterable[str],
    transcript: str,
    source_kind: str,
    source_id: str,
) -> dict[str, Any] | None:
    """Run v2 (catalog exact-lexical → pgvector top-K → critic) on the same
    `mentions` v1 just resolved, compare the resolved entity NAME sets, log a
    structured diff and RETURN it (or None when skipped / on error). Reuses
    the v1 Pass-1 `mentions` so there is NO extra extract call — the only
    added cost is embeddings + the critic. Writes nothing; swallows all
    errors (the return value is for tests/audit, callers may ignore it)."""
    t0 = time.time()
    try:
        api_key = getattr(settings, "openai_api_key", None)
        if not api_key:
            return None
        from openai import OpenAI

        from app.intent.llm_backends import OpenAIBackend

        client = OpenAI(api_key=api_key)
        embed_model = settings.embedding_model
        critic_model = settings.entity_match_critic_model
        embed_fn = make_openai_embed_fn(client, embed_model)
        backend = OpenAIBackend(client, critic_model)
        k = getattr(settings, "counterparty_match_v2_k", 20)

        lex, by_id = build_catalog_lexical_index(session)
        if not by_id:
            log.info(
                "counterparty_shadow_v2_skipped_empty_catalog",
                source_kind=source_kind, source_id=source_id,
            )
            return None

        def retrieve_fn(query: str, kk: int) -> list[dict[str, Any]]:
            return search_entities(
                session, kind=CATALOG_KIND, query_text=query,
                embed_fn=embed_fn, model=embed_model, k=kk,
            )

        v2_names: set[str] = set()
        n_exact = n_critic = n_none = 0
        for m in mentions:
            eid = lex.get(_normalise_name(m))
            if eid is not None:
                v2_names.add(by_id[eid].name)
                n_exact += 1
                continue
            res = match_entity(
                kind=CATALOG_KIND, mention=m,
                context=context_window(transcript, m),
                retrieve_fn=retrieve_fn, backend=backend,
                critic_model=critic_model, k=k,
            )
            mid = res.get("matched_entity_id")
            if mid and str(mid).isdigit() and int(mid) in by_id:
                v2_names.add(by_id[int(mid)].name)
                n_critic += 1
            else:
                n_none += 1

        v1n = {_normalise_name(n): n for n in v1_canonical_names if n}
        v2n = {_normalise_name(n): n for n in v2_names}
        both = sorted(v1n[key] for key in v1n.keys() & v2n.keys())
        v1_only = sorted(v1n[key] for key in v1n.keys() - v2n.keys())
        v2_only = sorted(v2n[key] for key in v2n.keys() - v1n.keys())
        result = {
            "mentions": len(mentions),
            "seconds": round(time.time() - t0, 1),
            "v2_exact": n_exact, "v2_critic": n_critic, "v2_none": n_none,
            "both": both, "v1_only": v1_only, "v2_only": v2_only,
        }
        log.info(
            "counterparty_shadow_v2",
            source_kind=source_kind, source_id=source_id,
            mentions=len(mentions), seconds=result["seconds"],
            v2_exact=n_exact, v2_critic=n_critic, v2_none=n_none,
            both=len(both), v1_only=len(v1_only), v2_only=len(v2_only),
            v1_only_sample=v1_only[:15], v2_only_sample=v2_only[:15],
        )
        return result
    except Exception as e:  # noqa: BLE001 — shadow must NEVER affect prod
        log.warning(
            "counterparty_shadow_v2_failed",
            source_kind=source_kind, source_id=source_id, error=str(e),
        )
        return None


__all__ = ["shadow_compare_v2"]
