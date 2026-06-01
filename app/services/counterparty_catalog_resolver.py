"""FR-CR-05-241 — shared resolver against the vector catalog.

ONE place that turns extracted mentions into catalog matches (exact-lexical →
pgvector top-K → critic), reading the SEPARATE pgvector catalog DB. Used by:
  * the SHADOW hook (counterparty_shadow_v2) — observe-only logging;
  * the LIVE "on" path — v2 as the single canonical resolver, mapping catalog
    matches back to the prod `counterparties` directory by normalised name.

Catalog vs prod directory: the catalog (entity_catalog_staging, ~4000) and the
prod `counterparties` directory (~847, periodically wiped-and-replaced from the
Google Sheet) are DIFFERENT id-spaces. The "on" path therefore maps a catalog
match to a counterparty by NORMALISED NAME and links ONLY to entities that
already exist in the directory — it NEVER auto-creates counterparties (the
Sheet sync is the directory's single source of truth; an auto-created row would
be wiped). A real catalog entity absent from the directory resolves to None →
review (operator adds it via the Sheet).
"""
from __future__ import annotations

from typing import Any

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


def open_catalog_session(fallback_session: Any) -> tuple[Any, bool]:
    """Return (session, owns). When CATALOG_DATABASE_URL is configured, open a
    read-only session on the separate pgvector catalog DB (owns=True, caller
    must close); otherwise reuse `fallback_session` (owns=False) — the ops
    sidecar / test case where catalog + app share one DB."""
    from app.db import get_catalog_session_factory

    factory = get_catalog_session_factory()
    if factory is not None:
        return factory(), True
    return fallback_session, False


def resolve_mentions_against_catalog(
    catalog_session: Any,
    *,
    settings: Any,
    mentions: list[str],
    transcript: str,
) -> dict[str, dict[str, Any]]:
    """Resolve each mention against the vector catalog. Returns
    {mention: {"entity_id": int, "name": str, "method": "exact"|"critic"}}
    for MATCHED mentions only (unmatched are omitted). Reads only the catalog
    session — does not touch the prod DB."""
    lex, by_id = build_catalog_lexical_index(catalog_session)
    if not by_id:
        return {}

    from openai import OpenAI

    from app.intent.llm_backends import OpenAIBackend

    client = OpenAI(api_key=settings.openai_api_key)
    embed_model = settings.embedding_model
    critic_model = settings.entity_match_critic_model
    embed_fn = make_openai_embed_fn(client, embed_model)
    backend = OpenAIBackend(client, critic_model)
    k = getattr(settings, "counterparty_match_v2_k", 20)

    def retrieve_fn(query: str, kk: int) -> list[dict[str, Any]]:
        return search_entities(
            catalog_session, kind=CATALOG_KIND, query_text=query,
            embed_fn=embed_fn, model=embed_model, k=kk,
        )

    out: dict[str, dict[str, Any]] = {}
    for m in mentions:
        eid = lex.get(_normalise_name(m))
        if eid is not None:
            out[m] = {"entity_id": eid, "name": by_id[eid].name, "method": "exact"}
            continue
        res = match_entity(
            kind=CATALOG_KIND, mention=m, context=context_window(transcript, m),
            retrieve_fn=retrieve_fn, backend=backend,
            critic_model=critic_model, k=k,
        )
        mid = res.get("matched_entity_id")
        if mid and str(mid).isdigit() and int(mid) in by_id:
            out[m] = {"entity_id": int(mid), "name": by_id[int(mid)].name,
                      "method": "critic"}
    return out


def resolve_mentions_to_directory_via_catalog(
    prod_session: Any,
    *,
    settings: Any,
    mentions: list[str],
    transcript: str,
    directory: list[Any],
) -> dict[str, int | None]:
    """LIVE "on" resolver. Resolve mentions against the vector catalog, then
    map each catalog match to a prod `counterparties` row by NORMALISED NAME.
    Returns {mention: counterparty_id | None}:
      * counterparty_id  — catalog match that EXISTS in the directory;
      * None             — no catalog match, OR a catalog match whose entity is
                           absent from the directory (→ review, never created).

    NEVER raises: on a catalog/LLM failure it logs and returns all-None so the
    pipeline keeps running (the meeting simply gets no counterparty links this
    run; an idempotent rerun links them once the catalog is reachable)."""
    result: dict[str, int | None] = {m: None for m in mentions}
    if not mentions:
        return result
    norm_to_id: dict[str, int] = {}
    for cp in directory:
        nn = getattr(cp, "name_normalised", None)
        if nn:
            norm_to_id.setdefault(nn, cp.id)

    session, owns = open_catalog_session(prod_session)
    try:
        matches = resolve_mentions_against_catalog(
            session, settings=settings, mentions=mentions, transcript=transcript,
        )
        linked = absent = 0
        for m, info in matches.items():
            cid = norm_to_id.get(_normalise_name(info["name"]))
            result[m] = cid
            if cid is not None:
                linked += 1
            else:
                absent += 1
                log.info(
                    "counterparty_v2_match_absent_from_directory",
                    mention=m, catalog_name=info["name"], method=info["method"],
                )
        log.info(
            "counterparty_resolve_v2_live",
            mentions=len(mentions), matched=len(matches),
            linked=linked, absent_to_review=absent,
            none=len(mentions) - len(matches),
        )
    except Exception as e:  # noqa: BLE001 — never break the pipeline
        log.error("counterparty_resolve_v2_live_failed", error=str(e))
        return {m: None for m in mentions}
    finally:
        if owns:
            session.rollback()
            session.close()
    return result


__all__ = [
    "open_catalog_session",
    "resolve_mentions_against_catalog",
    "resolve_mentions_to_directory_via_catalog",
]
