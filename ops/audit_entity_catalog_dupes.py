"""FR-CR-05-231 — read-only near-duplicate audit for the entity catalog.

The UNIQUE(name_normalised, is_org) constraint guarantees ZERO exact
duplicates. This tool surfaces *near* duplicates (slightly different
normalised forms — «Sebastian Thrun» vs «Sebastian Thrun Ph.D», a person
seen as both a block-1 contact and a block-2 profile) so the operator can
eyeball them before the catalog is swapped onto the live directory.

READ-ONLY: prints clusters, writes nothing, makes no LLM calls.

Usage:
    docker run --rm --network bridge --env-file .env -e DATABASE_URL=... \\
        slack-task-bot:pgv-test python -m ops.audit_entity_catalog_dupes \\
        [--threshold 0.86] [--limit 200]
"""
from __future__ import annotations

import argparse
import sys

from app.db import session_scope
from app.logging_setup import get_logger, setup_logging
from app.services.entity_catalog import (
    DUP_RATIO_THRESHOLD,
    find_duplicate_candidates,
    load_staging_items,
)

log = get_logger(__name__)


def main() -> int:
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=DUP_RATIO_THRESHOLD)
    ap.add_argument(
        "--limit", type=int, default=200,
        help="max clusters to print (largest first)",
    )
    args = ap.parse_args()

    with session_scope() as session:
        items = load_staging_items(session)
        clusters = find_duplicate_candidates(items, threshold=args.threshold)

    total = len(items)
    dup_rows = sum(len(c) for c in clusters)
    print(
        f"staging rows: {total} | "
        f"near-dup clusters: {len(clusters)} | "
        f"rows in clusters: {dup_rows} | threshold={args.threshold}"
    )
    if not clusters:
        print("No near-duplicate clusters found. ✓")
        return 0

    for i, cluster in enumerate(clusters[: args.limit], 1):
        kind = "ORG" if cluster[0]["is_org"] else "PERSON"
        print(f"\n[{i}] {kind} — {len(cluster)} entries:")
        for it in cluster:
            print(f"    id={it['id']:<6} «{it['name']}»  (norm: {it['name_normalised']})")
    if len(clusters) > args.limit:
        print(f"\n… {len(clusters) - args.limit} more clusters (raise --limit)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
