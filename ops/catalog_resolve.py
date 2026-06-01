"""FR-CR-05-239 — end-to-end entity resolution against the NEW dedup
catalog, hybrid: exact alias/name match → pgvector top-K → LLM critic.

operator plan 2026-06-01 step 2 («сначала акроним-алиасы, потом
end-to-end с критиком»). Reuses the prod stack — `extract_counterparty_
mentions` (Pass-1), `search_entities` (pgvector), `match_entity`
(LangGraph critic) — but pointed at `kind='catalog'` so we test the
cleaned 3998-entity catalog, not the live directory.

Why hybrid: the vector test showed terse acronyms buried in a long
text_repr rank poorly even when present as an alias. So FIRST try an
EXACT lexical match (mention ⟶ name_normalised OR any alias); only fall
back to vector+critic for fuzzy / contextual cases.

READ-ONLY. Input is either a transcript (--text-file, mentions are
extracted) or an explicit list (--mentions "a, b, c").

Usage:
    docker run --rm --network bridge -e DATABASE_URL="$DBURL" \\
      -e OPENAI_API_KEY=... slack-task-bot:pgv-test \\
      python -m ops.catalog_resolve --mentions "GS, a16z, Тезер, Шафлер, the softbank guys"

    docker run ... -v /tmp:/host ... python -m ops.catalog_resolve \\
      --text-file /host/transcript.txt --k 20
"""
from __future__ import annotations

import argparse
import sys

from openai import OpenAI
from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.intent.llm_backends import OpenAIBackend
from app.logging_setup import get_logger, setup_logging
from app.models.entity_catalog import EntityCatalogStaging
from app.services.counterparty_match import extract_counterparty_mentions
from app.services.entity_catalog import _normalise_name
from app.services.entity_embeddings import make_openai_embed_fn, search_entities
from app.services.entity_match_v2 import match_entity

log = get_logger(__name__)

KIND = "catalog"


def _build_lexical_index(session) -> tuple[dict[str, int], dict[int, EntityCatalogStaging]]:
    """norm(name)/norm(alias) → entity_id for UNAMBIGUOUS keys only.

    FR-CR-05-239 follow-up (A/B regression «Хёндай»): the aggressive
    Cyrillic alias pass put the same transliteration on several members
    of a brand family (Hyundai / Hyundai Motor / Hyundai Venture …). A
    coarse «max mentions_count wins» tiebreak then mis-routed the bare
    brand mention. Fix: a key that maps to MORE THAN ONE distinct entity
    is AMBIGUOUS — drop it from the exact layer so it falls through to
    vector+critic, which disambiguates with context."""
    rows = session.execute(select(EntityCatalogStaging)).scalars().all()
    by_id = {r.id: r for r in rows}
    key_to_ids: dict[str, set[int]] = {}
    def _put(key: str, r: EntityCatalogStaging) -> None:
        k = _normalise_name(key)
        if k:
            key_to_ids.setdefault(k, set()).add(r.id)
    for r in rows:
        _put(r.name, r)
        for a in (r.aliases or "").split(","):
            if a.strip():
                _put(a, r)
    idx = {k: next(iter(ids)) for k, ids in key_to_ids.items() if len(ids) == 1}
    return idx, by_id


def _context_for(transcript: str, mention: str, *, window: int = 300) -> str:
    i = transcript.lower().find(mention.lower())
    if i < 0:
        return mention
    a = max(0, i - window)
    return transcript[a:i + len(mention) + window]


def main() -> int:
    setup_logging()
    s = get_settings()
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--mentions", help="comma-separated mention surface forms")
    g.add_argument("--text-file", help="transcript file; mentions auto-extracted")
    ap.add_argument("--extract-model", default=s.openai_model)
    ap.add_argument("--critic-model", default=getattr(s, "entity_match_critic_model", "gpt-4o"))
    ap.add_argument("--embed-model", default="text-embedding-3-large")
    ap.add_argument("--k", type=int, default=20)  # 4000-target recall (operator: «k 12→20»)
    args = ap.parse_args()

    if not s.openai_api_key:
        print("ERROR: OPENAI_API_KEY not set", file=sys.stderr)
        return 2
    client = OpenAI(api_key=s.openai_api_key)
    backend = OpenAIBackend(client, args.extract_model)
    embed_fn = make_openai_embed_fn(client, args.embed_model)

    transcript = ""
    with session_scope() as session:
        if args.text_file:
            with open(args.text_file, encoding="utf-8") as fh:
                transcript = fh.read()
            mentions = extract_counterparty_mentions(
                transcript, llm_backend=backend, model=args.extract_model,
                reasoning_effort="high",
            )
            print(f"извлечено упоминаний: {len(mentions)} → {mentions}\n")
        else:
            mentions = [m.strip() for m in args.mentions.split(",") if m.strip()]

        lex, by_id = _build_lexical_index(session)

        def retrieve_fn(query: str, k: int):
            return search_entities(
                session, kind=KIND, query_text=query,
                embed_fn=embed_fn, model=args.embed_model, k=k,
            )

        n_exact = n_vec = n_none = 0
        for m in mentions:
            # 1) exact lexical (name or alias)
            eid = lex.get(_normalise_name(m))
            if eid is not None:
                r = by_id[eid]
                typ = "org" if r.is_org else "per"
                print(f"  «{m}»  →  [exact] {eid} «{r.name}» ({typ})")
                n_exact += 1
                continue
            # 2) vector top-K → critic
            ctx = _context_for(transcript, m) if transcript else m
            res = match_entity(
                kind=KIND, mention=m, context=ctx,
                retrieve_fn=retrieve_fn, backend=backend,
                critic_model=args.critic_model, k=args.k,
            )
            mid = res.get("matched_entity_id")
            conf = res.get("confidence", 0.0)
            if mid and str(mid).isdigit() and int(mid) in by_id:
                r = by_id[int(mid)]
                typ = "org" if r.is_org else "per"
                print(f"  «{m}»  →  [critic {conf:.2f}] {mid} «{r.name}» ({typ})")
                print(f"        {res.get('reasoning','')[:160]}")
                n_vec += 1
            else:
                cands = ", ".join(
                    f"{c.get('entity_id')}:{(by_id.get(int(c['entity_id'])).name if str(c.get('entity_id')).isdigit() and int(c['entity_id']) in by_id else '?')}"
                    for c in (res.get("candidates") or [])[:3]
                )
                print(f"  «{m}»  →  [none {conf:.2f}]  (top: {cands})")
                n_none += 1
        session.rollback()

    print(f"\nИТОГО: exact={n_exact}  critic={n_vec}  none={n_none}  всего={len(mentions)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
