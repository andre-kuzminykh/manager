"""FR-CR-05-240 — check a curated list of counterparty names against the
catalog and ADD the ones genuinely missing (operator list 2026-06-01).

Avoids creating near-dups: a name counts as PRESENT if it matches an
existing entity by (a) exact normalised name/alias, or (b) vector top-1
cosine >= --threshold (catches «EQT Group»→EQT, «Google Ventures»→GV,
«XTX»→XTX Markets). Only the rest are inserted.

DRY-RUN by default: prints FOUND (mapped) vs MISSING. --apply inserts the
MISSING as new staging rows (is_org=True default; refine later), with a
JSON backup of the inserted ids. After --apply, run enrich_entity_aliases
(--kind all, idempotent → only new) + catalog_vector_search embed.

Usage:
    docker run --rm --network bridge -e DATABASE_URL="$DBURL" \\
      -e OPENAI_API_KEY=... -v /tmp:/host slack-task-bot:pgv-test \\
      python -m ops.catalog_add_from_list --file /host/cp_list.txt
    # review MISSING, then add --apply
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time

from openai import OpenAI
from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.logging_setup import get_logger, setup_logging
from app.models.entity_catalog import EntityCatalogStaging
from app.services.entity_catalog import _normalise_name
from app.services.entity_embeddings import make_openai_embed_fn, search_entities

log = get_logger(__name__)

KIND = "catalog"
_NOISE = re.compile(r"\s*\((?:LEAD|Super LEAD|DEMO|meeting without Artem)\)\s*", re.I)


def _clean(line: str) -> str:
    s = line.strip().strip('"').strip()
    s = _NOISE.sub(" ", s)
    s = s.rstrip("?").strip()
    return re.sub(r"\s{2,}", " ", s)


def _lexical_index(session) -> dict[str, int]:
    idx: dict[str, int] = {}
    for r in session.execute(select(EntityCatalogStaging)).scalars().all():
        for key in (r.name, *(r.aliases or "").split(",")):
            k = _normalise_name(key)
            if k and k not in idx:
                idx[k] = r.id
    return idx


def main() -> int:
    setup_logging()
    s = get_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="one name per line")
    ap.add_argument("--threshold", type=float, default=0.90,
                    help="vector top-1 cosine to count as already-present")
    ap.add_argument("--embed-model", default="text-embedding-3-large")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup", default=f"/tmp/added_entities_{int(time.time())}.json")
    args = ap.parse_args()

    if not s.openai_api_key:
        print("ERROR: OPENAI_API_KEY not set", file=sys.stderr)
        return 2
    embed_fn = make_openai_embed_fn(OpenAI(api_key=s.openai_api_key), args.embed_model)

    with open(args.file, encoding="utf-8") as fh:
        raw = [ln for ln in fh.read().splitlines()]
    names, seen = [], set()
    for ln in raw:
        c = _clean(ln)
        if c and c.lower() not in seen:
            seen.add(c.lower())
            names.append(c)
    print(f"вход: {len(raw)} строк → {len(names)} уникальных имён\n")

    with session_scope() as session:
        lex = _lexical_index(session)
        found_exact, found_vec, missing = [], [], []
        for nm in names:
            key = _normalise_name(nm)
            if key and key in lex:
                found_exact.append((nm, lex[key])); continue
            hits = search_entities(session, kind=KIND, query_text=nm,
                                   embed_fn=embed_fn, model=args.embed_model, k=1)
            if hits and hits[0]["score"] >= args.threshold:
                found_vec.append((nm, int(hits[0]["entity_id"]), hits[0]["score"]))
            else:
                missing.append(nm)

        by_id = {r.id: r.name for r in session.execute(
            select(EntityCatalogStaging)).scalars().all()}

        print(f"=== УЖЕ ЕСТЬ (exact: {len(found_exact)}) ===")
        for nm, eid in found_exact:
            print(f"  «{nm}» = {eid} «{by_id.get(eid)}»")
        print(f"\n=== УЖЕ ЕСТЬ (vector≥{args.threshold}: {len(found_vec)}) — свериться ===")
        for nm, eid, sc in found_vec:
            print(f"  «{nm}» ≈ {sc:.2f} → {eid} «{by_id.get(eid)}»")
        print(f"\n=== НЕТ в каталоге (добавим: {len(missing)}) ===")
        for nm in missing:
            print(f"  + {nm}")

        if not args.apply:
            print(f"\n(dry-run — ничего не добавлено. exact={len(found_exact)} "
                  f"vector={len(found_vec)} missing={len(missing)})")
            session.rollback()
            return 0

        added = []
        for nm in missing:
            row = EntityCatalogStaging(
                name=nm, name_normalised=_normalise_name(nm),
                is_org=True, description="", aliases=None, mentions_count=0,
            )
            session.add(row); session.flush()
            added.append({"id": row.id, "name": nm})
        with open(args.backup, "w", encoding="utf-8") as fh:
            json.dump({"ts": time.time(), "added": added}, fh, ensure_ascii=False, indent=2)
        session.commit()
        print(f"\n[apply] добавлено: {len(added)} (бэкап ids: {args.backup})")
        print("дальше: enrich_entity_aliases --kind all  +  catalog_vector_search embed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
