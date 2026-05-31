"""FR-CR-05-224 — entity-match REVIEW harness (read-only, no writes).

Takes one Zoom recording (or the latest), extracts counterparty
mentions from its transcript, then resolves each mention BOTH ways:

  v1 = current production path (whole directory → LLM, resolve_mentions_to_directory)
  v2 = new pgvector top-K retrieval → LangGraph critic (entity_match_v2)

and prints a side-by-side mapping so the operator can review
«какие сущности извлеклись и на какие заменились, и почему» before any
rollout. Writes a JSON report to --out (default /tmp/entity_match_review.json).

READ-ONLY: never writes to the DB. Safe to run against prod or the
isolated pgvector test DB.

Usage:
    docker exec <bot> python -m ops.review_entity_match --zoom-id <ZOOM_ID>
    docker exec <bot> python -m ops.review_entity_match --latest
"""
from __future__ import annotations

import argparse
import json
import sys

from openai import OpenAI

from app.config import get_settings
from app.db import session_scope
from app.logging_setup import get_logger, setup_logging
from app.models import Counterparty
from app.models.entity_embedding import KIND_COUNTERPARTY
from app.models.zoom import ZoomRecording
from app.services.counterparty_match import (
    extract_counterparty_mentions,
    resolve_mentions_to_directory,
)
from app.services.entity_embeddings import make_openai_embed_fn, search_entities
from app.services.entity_match_v2 import match_entity

log = get_logger(__name__)


def _context_for(transcript: str, mention: str, *, window: int = 350) -> str:
    """Return a snippet of transcript around the first occurrence of the
    mention (so the critic has local context). Falls back to head."""
    idx = transcript.lower().find(mention.lower())
    if idx < 0:
        return transcript[:window]
    lo = max(0, idx - window)
    hi = min(len(transcript), idx + len(mention) + window)
    return transcript[lo:hi]


def main() -> int:
    setup_logging()
    s = get_settings()
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--zoom-id", help="ZoomRecording.zoom_id to review")
    g.add_argument("--latest", action="store_true", help="use most recent zoom recording")
    ap.add_argument("--out", default="/tmp/entity_match_review.json")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--embed-model", default=s.embedding_model)
    ap.add_argument("--critic-model", default=s.fireflies_tasks_model)
    args = ap.parse_args()

    if not s.openai_api_key:
        print("ERROR: OPENAI_API_KEY not configured", file=sys.stderr)
        return 2

    client = OpenAI(api_key=s.openai_api_key)
    embed_fn = make_openai_embed_fn(client, model=args.embed_model)
    from app.intent.llm_backends import OpenAIBackend

    backend = OpenAIBackend(client, s.openai_model)

    with session_scope() as session:
        if args.latest:
            row = (
                session.query(ZoomRecording)
                .filter(ZoomRecording.transcript_text.isnot(None))
                .order_by(ZoomRecording.created_at.desc())
                .first()
            )
        else:
            row = (
                session.query(ZoomRecording)
                .filter(ZoomRecording.zoom_id == args.zoom_id)
                .first()
            )
        if not row:
            print("ERROR: zoom recording not found", file=sys.stderr)
            return 1
        if not row.transcript_text:
            print(f"ERROR: zoom {row.zoom_id} has no transcript", file=sys.stderr)
            return 1

        transcript = row.transcript_text
        print(f"=== ZOOM {row.zoom_id} (created {row.created_at}) ===")
        print(f"transcript length: {len(transcript)} chars")

        # --- extract mentions (shared Pass-1) ---
        mentions = extract_counterparty_mentions(
            transcript, llm_backend=backend, model=args.critic_model,
            trace_source="review", trace_recording_id=row.zoom_id,
        )
        print(f"extracted {len(mentions)} counterparty mentions: {mentions}")

        # --- v1: whole-directory resolve ---
        directory = session.query(Counterparty).order_by(Counterparty.name).all()
        id_to_name = {c.id: c.name for c in directory}
        v1 = resolve_mentions_to_directory(
            mentions, directory, llm_backend=backend, model=args.critic_model,
            trace_source="review", trace_recording_id=row.zoom_id,
        )

        # --- v2: pgvector top-K + critic ---
        def retrieve_fn(query: str, k: int):
            return search_entities(
                session, kind=KIND_COUNTERPARTY, query_text=query,
                embed_fn=embed_fn, model=args.embed_model, k=k,
            )

        report = []
        for m in mentions:
            v2 = match_entity(
                kind=KIND_COUNTERPARTY, mention=m,
                context=_context_for(transcript, m),
                retrieve_fn=retrieve_fn, backend=backend,
                critic_model=args.critic_model, k=args.k,
            )
            v1_id = v1.get(m)
            v2_id = int(v2["matched_entity_id"]) if v2.get("matched_entity_id") else None
            report.append({
                "mention": m,
                "v1_id": v1_id,
                "v1_name": id_to_name.get(v1_id),
                "v2_id": v2_id,
                "v2_name": id_to_name.get(v2_id),
                "v2_confidence": v2["confidence"],
                "v2_reasoning": v2["reasoning"],
                "v2_attempts": v2["attempts"],
                "v2_top_candidates": [
                    {"id": c["entity_id"], "name": id_to_name.get(int(c["entity_id"]))
                     if str(c["entity_id"]).isdigit() else None,
                     "score": round(c["score"], 3)}
                    for c in v2.get("candidates", [])[:5]
                ],
            })

    # --- human-readable table ---
    print("\n" + "=" * 90)
    print(f"{'MENTION':<22} {'v1 → подставил':<26} {'v2 → подставил':<26} {'conf':>5}")
    print("-" * 90)
    agree = diff = 0
    for r in report:
        same = r["v1_id"] == r["v2_id"]
        agree += int(same)
        diff += int(not same)
        flag = "" if same else "  ◀ DIFF"
        print(
            f"{(r['mention'] or '')[:21]:<22} "
            f"{(str(r['v1_name']) or '—')[:25]:<26} "
            f"{(str(r['v2_name']) or '—')[:25]:<26} "
            f"{r['v2_confidence']:>5.2f}{flag}"
        )
    print("-" * 90)
    print(f"mentions={len(report)}  agree={agree}  diff={diff}")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"zoom_id": args.zoom_id or "latest", "mentions": report}, f,
                  ensure_ascii=False, indent=2)
    print(f"\nfull report → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
