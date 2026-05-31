"""FR-CR-05-221 — refresh directory entity embeddings (cron-batch).

Scans counterparties / team_members / employees, (re)embeds only the
rows whose `text_repr` changed since last run (hash check in
`entity_embeddings`), and upserts. Idempotent: a run with no source
changes makes ZERO OpenAI calls.

Usage:
    docker exec <bot> python -m ops.refresh_entity_embeddings \\
        [--kinds counterparty,team_member,employee] \\
        [--model text-embedding-3-large] [--batch-size 256] [--dry-run]

Cron (every 10 min, after directory syncs):
    */10 * * * * docker exec <bot> python -m ops.refresh_entity_embeddings
"""
from __future__ import annotations

import argparse
import sys

from openai import OpenAI

from app.config import get_settings
from app.db import session_scope
from app.logging_setup import get_logger, setup_logging
from app.services.entity_embeddings import (
    DEFAULT_EMBED_MODEL,
    KIND_COUNTERPARTY,
    KIND_EMPLOYEE,
    KIND_TEAM_MEMBER,
    collect_entity_texts,
    make_openai_embed_fn,
    refresh_embeddings,
)

log = get_logger(__name__)

_ALL_KINDS = (KIND_COUNTERPARTY, KIND_TEAM_MEMBER, KIND_EMPLOYEE)


def main() -> int:
    setup_logging()
    s = get_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--kinds",
        default=",".join(_ALL_KINDS),
        help="comma-separated subset of: " + ",".join(_ALL_KINDS),
    )
    ap.add_argument("--model", default=getattr(s, "embedding_model", "") or DEFAULT_EMBED_MODEL)
    ap.add_argument("--batch-size", type=int, default=256)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="only count entities + how many would (re)embed; no OpenAI calls, no writes",
    )
    args = ap.parse_args()

    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    bad = [k for k in kinds if k not in _ALL_KINDS]
    if bad:
        print(f"ERROR: unknown kinds {bad}; allowed {_ALL_KINDS}", file=sys.stderr)
        return 2

    if args.dry_run:
        with session_scope() as session:
            rows = collect_entity_texts(session, kinds=kinds)
            by_kind: dict[str, int] = {}
            for kind, _eid, _tr in rows:
                by_kind[kind] = by_kind.get(kind, 0) + 1
        print(f"DRY-RUN: collected {len(rows)} entities to embed (model={args.model})")
        for k in kinds:
            print(f"  {k}: {by_kind.get(k, 0)}")
        return 0

    if not s.openai_api_key:
        print("ERROR: OPENAI_API_KEY not configured", file=sys.stderr)
        return 2

    embed_fn = make_openai_embed_fn(
        OpenAI(api_key=s.openai_api_key), model=args.model
    )
    with session_scope() as session:
        stats = refresh_embeddings(
            session,
            embed_fn=embed_fn,
            kinds=kinds,
            model=args.model,
            batch_size=args.batch_size,
        )
        session.commit()

    print(
        f"DONE: scanned={stats['scanned']} embedded={stats['embedded']} "
        f"skipped={stats['skipped']} (model={args.model})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
