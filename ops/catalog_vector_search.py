"""FR-CR-05-237 — test-bench vector search OVER THE NEW dedup catalog
(`entity_catalog_staging`), separate from the live `counterparties`
embeddings (kind=counterparty). Operator 2026-06-01: «мне нужно для
начала с ним организовать векторный поиск тестово».

Reuses the production embedding satellite (`entity_embeddings`,
pgvector 3072, text-embedding-3-large) and its cosine search — just a
NEW `kind='catalog'` whose `entity_id` is the staging row id. So the
clean 3998-entity catalog gets its own searchable vector space without
touching the live directory.

Two subcommands:
  embed   — (re)embed every staging row. text_repr = name + aliases +
            (parent_org) + description. Idempotent: skips rows whose
            text_repr hash is unchanged (no OpenAI call). --reset wipes
            kind=catalog first.
  search  — embed a query string, print top-K nearest catalog entities
            (id, name, org/per, cosine score, description snippet).

Usage (against the pgvector test DB):
    docker run --rm --network bridge -e DATABASE_URL="$DBURL" \\
      -e OPENAI_API_KEY=... slack-task-bot:pgv-test \\
      python -m ops.catalog_vector_search embed [--reset] [--batch 128]

    docker run --rm --network bridge -e DATABASE_URL="$DBURL" \\
      -e OPENAI_API_KEY=... slack-task-bot:pgv-test \\
      python -m ops.catalog_vector_search search --query "GS PWM" -k 10
"""
from __future__ import annotations

import argparse
import sys

from openai import OpenAI
from sqlalchemy import select, text as sql_text

from app.config import get_settings
from app.db import get_engine, session_scope
from app.logging_setup import get_logger, setup_logging
from app.models.entity_catalog import EntityCatalogStaging
from app.models.entity_embedding import EntityEmbedding
from app.services.entity_embeddings import (
    DEFAULT_EMBED_MODEL,
    _upsert_embedding,
    make_openai_embed_fn,
    search_entities,
    text_repr_hash,
)

log = get_logger(__name__)

KIND = "catalog"


def _text_repr(r: EntityCatalogStaging) -> str:
    """Compact, embed-friendly representation of a catalog entity."""
    parts = [r.name.strip()]
    kind = "organization" if r.is_org else "person"
    parts.append(f"({kind})")
    if (r.aliases or "").strip():
        parts.append("aka " + r.aliases.strip())
    if (r.parent_org or "").strip():
        parts.append("part of " + r.parent_org.strip())
    if (r.description or "").strip():
        parts.append("— " + " ".join(r.description.split())[:400])
    return " ".join(parts)


def _ensure_table() -> None:
    """Create the vector extension + entity_embeddings table if the
    isolated test DB doesn't have them yet (prod already does)."""
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(sql_text("CREATE EXTENSION IF NOT EXISTS vector"))
    EntityEmbedding.__table__.create(bind=eng, checkfirst=True)


def cmd_embed(args) -> int:
    s = get_settings()
    if not s.openai_api_key:
        print("ERROR: OPENAI_API_KEY not set", file=sys.stderr)
        return 2
    _ensure_table()
    embed_fn = make_openai_embed_fn(OpenAI(api_key=s.openai_api_key), args.model)

    with session_scope() as session:
        if args.reset:
            n = session.execute(
                sql_text("DELETE FROM entity_embeddings WHERE kind = :k"),
                {"k": KIND},
            ).rowcount
            session.commit()
            print(f"[reset] удалено старых эмбеддингов kind=catalog: {n}")

        existing = {
            eid: h for eid, h in session.execute(
                select(EntityEmbedding.entity_id, EntityEmbedding.text_repr_hash)
                .where(EntityEmbedding.kind == KIND)
                .where(EntityEmbedding.model == args.model)
            ).all()
        }
        rows = session.execute(select(EntityCatalogStaging)).scalars().all()
        todo: list[tuple[str, str]] = []  # (entity_id, text_repr)
        for r in rows:
            tr = _text_repr(r)
            h = text_repr_hash(tr)
            if existing.get(str(r.id)) == h:
                continue
            todo.append((str(r.id), tr))

        print(f"каталог: {len(rows)} | к (пере)эмбеддингу: {len(todo)} "
              f"| без изменений: {len(rows) - len(todo)}")
        done = 0
        for i in range(0, len(todo), args.batch):
            chunk = todo[i:i + args.batch]
            vecs = embed_fn([tr for _, tr in chunk])
            if len(vecs) != len(chunk):
                print(f"WARN: embed вернул {len(vecs)} для {len(chunk)}", file=sys.stderr)
            for (eid, tr), vec in zip(chunk, vecs):
                _upsert_embedding(
                    session, kind=KIND, entity_id=eid, model=args.model,
                    vec=vec, text_repr=tr, text_repr_hash=text_repr_hash(tr),
                )
            session.commit()
            done += len(chunk)
            print(f"  …{done}/{len(todo)}")
        total = session.execute(
            sql_text("SELECT count(*) FROM entity_embeddings WHERE kind=:k"),
            {"k": KIND},
        ).scalar()
        print(f"[ok] эмбеддингов kind=catalog в БД: {total}")
    return 0


def cmd_search(args) -> int:
    s = get_settings()
    if not s.openai_api_key:
        print("ERROR: OPENAI_API_KEY not set", file=sys.stderr)
        return 2
    embed_fn = make_openai_embed_fn(OpenAI(api_key=s.openai_api_key), args.model)
    with session_scope() as session:
        hits = search_entities(
            session, kind=KIND, query_text=args.query,
            embed_fn=embed_fn, model=args.model, k=args.k,
        )
        if not hits:
            print("(пусто — сначала прогони `embed`)")
            return 0
        ids = [int(h["entity_id"]) for h in hits]
        meta = {
            r.id: r for r in session.execute(
                select(EntityCatalogStaging).where(EntityCatalogStaging.id.in_(ids))
            ).scalars().all()
        }
        print(f"query: {args.query!r}\n")
        for rank, h in enumerate(hits, 1):
            r = meta.get(int(h["entity_id"]))
            typ = ("org" if r and r.is_org else "per")
            nm = r.name if r else "?"
            descr = (r.description or "").split("\n")[0][:70] if r else ""
            print(f"  {rank:2d}. {h['score']:.3f}  [{typ}] {h['entity_id']:>5} «{nm}»"
                  + (f"  — {descr}" if descr else ""))
        session.rollback()
    return 0


def main() -> int:
    setup_logging()
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("embed", help="(re)embed entity_catalog_staging")
    pe.add_argument("--model", default=DEFAULT_EMBED_MODEL)
    pe.add_argument("--batch", type=int, default=128)
    pe.add_argument("--reset", action="store_true")
    pe.set_defaults(fn=cmd_embed)

    ps = sub.add_parser("search", help="top-K nearest catalog entities")
    ps.add_argument("--query", required=True)
    ps.add_argument("-k", type=int, default=10)
    ps.add_argument("--model", default=DEFAULT_EMBED_MODEL)
    ps.set_defaults(fn=cmd_search)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
