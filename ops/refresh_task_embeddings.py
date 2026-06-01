"""FR-TV (P1) — refresh TASK + TEAM embeddings into the separate vector DB.

Cross-DB cron batch: reads source rows (tasks + team_members + employees) from
the PRIMARY transactional DB and (re)embeds only the rows whose `text_repr`
changed since last run (hash check), upserting into the SEPARATE pgvector
instance (`TASK_VECTOR_DATABASE_URL` → `CATALOG_DATABASE_URL`). The prod
transactional DB is never written to. Idempotent: a run with no source changes
makes ZERO OpenAI calls.

CONTENT-only text_repr for tasks (status/due read live at query time, NOT
embedded — FR-TV-010/014), so a pure status change never re-embeds.

Usage (prod):
    docker run --rm --network manager_default --env-file ... \\
      -e DATABASE_URL=<primary> -e TASK_VECTOR_DATABASE_URL=<pgvector> \\
      -e OPENAI_API_KEY=... manager-bot:v2shadow \\
      python -m ops.refresh_task_embeddings [--kinds task,team_member,employee] \\
      [--model ...] [--batch-size 256] [--dry-run]

Cron (every 10 min):
    */10 * * * * docker run ... python -m ops.refresh_task_embeddings
"""
from __future__ import annotations

import argparse
import sys

from openai import OpenAI

from app.config import get_settings
from app.db import get_task_vector_session_factory, session_scope
from app.logging_setup import get_logger, setup_logging
from app.services.entity_embeddings import (
    DEFAULT_EMBED_MODEL,
    KIND_EMPLOYEE,
    KIND_TASK,
    KIND_TEAM_MEMBER,
    collect_entity_texts,
    make_openai_embed_fn,
    refresh_embeddings_cross_db,
)

log = get_logger(__name__)

# Tasks + the team in one pass (team is needed for owner resolution, FR-TV-015).
_ALL_KINDS = (KIND_TASK, KIND_TEAM_MEMBER, KIND_EMPLOYEE)


def main() -> int:
    setup_logging()
    s = get_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--kinds",
        default=",".join(_ALL_KINDS),
        help="comma-separated subset of: " + ",".join(_ALL_KINDS),
    )
    ap.add_argument(
        "--model",
        default=getattr(s, "task_vector_model", "") or DEFAULT_EMBED_MODEL,
    )
    ap.add_argument("--batch-size", type=int, default=256)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="count source rows only; no OpenAI calls, no writes",
    )
    args = ap.parse_args()

    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    bad = [k for k in kinds if k not in _ALL_KINDS]
    if bad:
        print(f"ERROR: unknown kinds {bad}; allowed {_ALL_KINDS}", file=sys.stderr)
        return 2

    # --- DRY RUN: read source only, report counts per kind ---
    if args.dry_run:
        with session_scope() as src:
            rows = collect_entity_texts(src, kinds=kinds)
            by_kind: dict[str, int] = {}
            for kind, _eid, _tr in rows:
                by_kind[kind] = by_kind.get(kind, 0) + 1
            src.rollback()
        log.info("task_embeddings_dry_run", total=len(rows), by_kind=by_kind,
                 model=args.model)
        print(f"DRY-RUN: {len(rows)} source rows (model={args.model})")
        for k in kinds:
            print(f"  {k}: {by_kind.get(k, 0)}")
        return 0

    if not s.openai_api_key:
        print("ERROR: OPENAI_API_KEY not configured", file=sys.stderr)
        return 2

    target_factory = get_task_vector_session_factory()
    if target_factory is None:
        print("ERROR: neither TASK_VECTOR_DATABASE_URL nor CATALOG_DATABASE_URL "
              "is set — nowhere to write embeddings", file=sys.stderr)
        return 2

    embed_fn = make_openai_embed_fn(
        OpenAI(api_key=s.openai_api_key), model=args.model
    )

    log.info("task_embeddings_refresh_started", kinds=kinds, model=args.model,
             batch_size=args.batch_size)
    # source = primary DB (tasks/team); target = separate pgvector instance.
    target = target_factory()
    try:
        with session_scope() as src:
            stats = refresh_embeddings_cross_db(
                src, target,
                embed_fn=embed_fn, kinds=kinds, model=args.model,
                batch_size=args.batch_size,
            )
            src.rollback()  # source is read-only here
        target.commit()
    except Exception:
        target.rollback()
        log.error("task_embeddings_refresh_failed", kinds=kinds, model=args.model)
        raise
    finally:
        target.close()

    log.info("task_embeddings_refresh_done", **stats, model=args.model)
    print(
        f"DONE: scanned={stats['scanned']} embedded={stats['embedded']} "
        f"skipped={stats['skipped']} pruned={stats['pruned']} (model={args.model})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
