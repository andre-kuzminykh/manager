"""FR-CR-05-231 — build the deduplicated entity catalog from the export.

Reads the operator's tab-separated dump, extracts entities chunk-by-chunk
with an LLM, and accumulates a deduplicated catalog in
``entity_catalog_staging`` (with a per-chunk checkpoint in
``entity_catalog_ingest`` so the run is resumable / idempotent).

STAGING ONLY — never touches the live directory. Intended to run against
the isolated pgvector test DB first («сначала временную, потом заменим»).

Usage (a few chunks at a time so context/cost stay bounded):
    docker exec <bot> python -m ops.build_entity_catalog \\
        --source /path/to/export.txt \\
        [--model gpt-4o] [--max-chars 6000] \\
        [--limit-chunks 25] [--start-chunk 0] \\
        [--reasoning high] [--dry-run]

Re-running picks up where it left off (finished chunks are skipped).
Pass --reset to wipe staging + ledger and start clean.
"""
from __future__ import annotations

import argparse
import sys

from openai import OpenAI

from app.config import get_settings
from app.db import session_scope
from app.intent.llm_backends import OpenAIBackend
from app.logging_setup import get_logger, setup_logging
from app.services.entity_catalog import (
    DEFAULT_MAX_CHARS,
    ingest_source,
    iter_source_chunks,
)

log = get_logger(__name__)


def main() -> int:
    setup_logging()
    s = get_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="path to the TSV export")
    ap.add_argument(
        "--model",
        default=getattr(s, "openai_title_model", None) or "gpt-4o",
        help="extraction model (default: openai_title_model / gpt-4o)",
    )
    ap.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    ap.add_argument("--start-chunk", type=int, default=0)
    ap.add_argument(
        "--limit-chunks", type=int, default=None,
        help="process at most N new chunks this run (default: all remaining)",
    )
    ap.add_argument(
        "--reasoning", default=None,
        help="reasoning_effort passthrough for reasoning models (e.g. high)",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="count chunks/rows only; no LLM calls, no writes",
    )
    ap.add_argument(
        "--reset", action="store_true",
        help="TRUNCATE staging + ingest ledger before building",
    )
    args = ap.parse_args()

    # Quick local sanity: how many chunks does this file/budget produce?
    total = sum(1 for _ in iter_source_chunks(args.source, max_chars=args.max_chars))
    log.info(
        "entity_catalog_plan",
        source=args.source, max_chars=args.max_chars,
        total_chunks=total, model=args.model, dry_run=args.dry_run,
    )

    if args.dry_run:
        with session_scope() as session:
            stats = ingest_source(
                session, path=args.source,
                llm_backend=None,  # unused in dry-run
                model=args.model, max_chars=args.max_chars,
                start_chunk=args.start_chunk, limit_chunks=args.limit_chunks,
                dry_run=True,
            )
        log.info("entity_catalog_dry_run", **stats)
        print(stats)
        return 0

    backend = OpenAIBackend(client=OpenAI(api_key=s.openai_api_key), model=args.model)

    with session_scope() as session:
        if args.reset:
            from app.models.entity_catalog import (
                EntityCatalogIngest,
                EntityCatalogStaging,
            )

            session.query(EntityCatalogStaging).delete()
            session.query(EntityCatalogIngest).delete()
            session.flush()
            log.info("entity_catalog_reset_done")

        def _commit() -> None:
            session.commit()

        stats = ingest_source(
            session, path=args.source, llm_backend=backend,
            model=args.model, max_chars=args.max_chars,
            start_chunk=args.start_chunk, limit_chunks=args.limit_chunks,
            reasoning_effort=args.reasoning, commit=_commit,
        )

    log.info("entity_catalog_done", **stats)
    print(stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
