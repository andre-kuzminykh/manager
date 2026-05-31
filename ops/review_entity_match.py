"""FR-CR-05-228 — entity-match END-TO-END review (Zoom + Fireflies).

Read-only. Per source (zoom / fireflies / both), takes the LATEST recording
and walks the full trace:

  1. transcript stats + saved detailed_summary (the prod artifact)
  2. extract counterparty mentions from transcript (Pass-1, gpt-5.5 high)
  3. resolve each mention BOTH ways:
       v1 = current prod (whole directory → LLM)
       v2 = pgvector top-K → LangGraph critic (gpt-4o)
  4. compare with what is ALREADY saved in the DB
     (CounterpartyMention by source_kind+source_id)
  5. for each mention, print a transcript snippet + v1 / v2 / saved-in-DB
  6. tail: counts agree / diff / v2-novel, and which counterparties
     SHOULD be in the saved summary but aren't (or vice versa)

Writes nothing to the DB. Safe to run on prod or on the isolated
pgvector test DB.

Usage:
    docker exec <bot> python -m ops.review_entity_match \\
        --latest --source both \\
        [--limit 20] [--skip-v1] [--out /tmp/trace.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from openai import OpenAI

from app.config import get_settings
from app.db import session_scope
from app.intent.llm_backends import OpenAIBackend
from app.logging_setup import get_logger, setup_logging
from app.models import Counterparty
from app.models.counterparty import CounterpartyMention
from app.models.entity_embedding import KIND_COUNTERPARTY
from app.models.fireflies import MeetingRecording
from app.models.zoom import ZoomRecording
from app.services.counterparty_match import (
    extract_counterparty_mentions,
    resolve_mentions_to_directory,
)
from app.services.entity_embeddings import make_openai_embed_fn, search_entities
from app.services.entity_match_v2 import match_entity

log = get_logger(__name__)


def _context_for(transcript: str, mention: str, *, window: int = 350) -> str:
    idx = transcript.lower().find(mention.lower())
    if idx < 0:
        return transcript[:window]
    lo = max(0, idx - window)
    hi = min(len(transcript), idx + len(mention) + window)
    return transcript[lo:hi]


def _summary_hit_count(summary: str | None, names: list[str]) -> dict[str, int]:
    """How many times each canonical name appears in the saved summary
    (case-insensitive, substring). Tells us which matched entities
    actually show up in the user-facing artefact."""
    out: dict[str, int] = {}
    if not summary:
        return out
    low = summary.lower()
    for n in names:
        if not n:
            continue
        out[n] = low.count(n.lower())
    return out


def _walk_one(
    *,
    session,
    kind: str,
    rec,
    transcript: str,
    summary: str | None,
    args,
    backend,
    embed_fn,
    directory,
    id_to_name,
):
    """Walk one recording end-to-end. Returns the structured trace dict."""
    rec_id = rec.zoom_id if kind == "zoom" else rec.fireflies_id
    created = rec.created_at.isoformat() if rec.created_at else None
    print(f"\n{'='*92}")
    print(f"=== {kind.upper()} {rec_id}  (created {created})")
    print(f"=== transcript: {len(transcript)} chars, summary: {len(summary or '')} chars")
    print(f"{'='*92}")

    # ----- Pass-1: extract mentions -----
    mentions = extract_counterparty_mentions(
        transcript, llm_backend=backend, model=args.extract_model,
        reasoning_effort=(args.extract_reasoning or None),
        trace_source="review", trace_recording_id=rec_id,
    )
    print(f"\nextracted {len(mentions)} mentions: {mentions}")
    if args.limit and args.limit > 0:
        mentions = mentions[: args.limit]
        print(f"--limit {args.limit} → reviewing first {len(mentions)}")

    # ----- v1 baseline (skippable) -----
    if args.skip_v1:
        v1 = {}
    else:
        v1 = resolve_mentions_to_directory(
            mentions, directory, llm_backend=backend, model=args.extract_model,
            trace_source="review", trace_recording_id=rec_id,
        )

    # ----- saved CounterpartyMention rows (what prod ACTUALLY recorded) -----
    saved_rows = (
        session.query(CounterpartyMention)
        .filter(
            CounterpartyMention.source_kind == kind,
            CounterpartyMention.source_id == rec_id,
        )
        .all()
    )
    saved_cp_ids = {r.counterparty_id for r in saved_rows}
    saved_names_by_id = {r.counterparty_id: id_to_name.get(r.counterparty_id) for r in saved_rows}
    print(f"\nALREADY in DB (counterparty_mentions): {len(saved_rows)} rows → "
          f"{[saved_names_by_id[i] for i in saved_cp_ids]}")

    # ----- v2: pgvector top-K + critic, per mention -----
    def retrieve_fn(query: str, k: int):
        return search_entities(
            session, kind=KIND_COUNTERPARTY, query_text=query,
            embed_fn=embed_fn, model=args.embed_model, k=k,
        )

    trace = []
    for m in mentions:
        v1_id = v1.get(m)
        v2 = match_entity(
            kind=KIND_COUNTERPARTY, mention=m,
            context=_context_for(transcript, m),
            retrieve_fn=retrieve_fn, backend=backend,
            critic_model=args.critic_model, k=args.k,
        )
        v2_id = (int(v2["matched_entity_id"])
                 if v2.get("matched_entity_id") and str(v2["matched_entity_id"]).isdigit()
                 else None)
        snippet = _context_for(transcript, m, window=80)
        trace.append({
            "mention": m,
            "transcript_snippet": snippet,
            "v1_id": v1_id, "v1_name": id_to_name.get(v1_id),
            "v2_id": v2_id, "v2_name": id_to_name.get(v2_id),
            "v2_confidence": v2["confidence"],
            "v2_reasoning": v2["reasoning"],
            "v2_attempts": v2["attempts"],
            "v2_top_candidates": [
                {"id": c["entity_id"],
                 "name": id_to_name.get(int(c["entity_id"]))
                         if str(c["entity_id"]).isdigit() else None,
                 "score": round(c["score"], 3)}
                for c in v2.get("candidates", [])[:5]
            ],
            "in_db": v1_id in saved_cp_ids or v2_id in saved_cp_ids,
            "in_db_via": ("v1" if v1_id in saved_cp_ids else
                          "v2" if v2_id in saved_cp_ids else None),
        })

    # ----- print per-mention trace -----
    print(f"\n{'─'*92}")
    print(f"{'#':>3}  {'MENTION':<22} {'v1 →':<22} {'v2 →':<22} {'conf':>5}  db?")
    print(f"{'─'*92}")
    agree = diff = v2_novel = 0
    for i, t in enumerate(trace, 1):
        same = t['v1_id'] == t['v2_id']
        agree += same; diff += not same
        if t['v2_id'] is not None and not t['in_db']:
            v2_novel += 1
        db_mark = ("✓" if t['in_db'] else "·")
        print(f"{i:>3}  {(t['mention'] or '')[:21]:<22} "
              f"{(str(t['v1_name']) or '—')[:21]:<22} "
              f"{(str(t['v2_name']) or '—')[:21]:<22} "
              f"{t['v2_confidence']:>5.2f}  {db_mark}")
    print(f"{'─'*92}")
    print(f"mentions={len(trace)}  agree={agree}  diff={diff}  "
          f"v2-novel (not yet in DB)={v2_novel}")

    # ----- summary coverage -----
    v1_names = list({t['v1_name'] for t in trace if t['v1_name']})
    v2_names = list({t['v2_name'] for t in trace if t['v2_name']})
    hits_v1 = _summary_hit_count(summary, v1_names)
    hits_v2 = _summary_hit_count(summary, v2_names)
    in_sum_v1 = [n for n, c in hits_v1.items() if c > 0]
    miss_sum_v1 = [n for n, c in hits_v1.items() if c == 0]
    in_sum_v2 = [n for n, c in hits_v2.items() if c > 0]
    miss_sum_v2 = [n for n, c in hits_v2.items() if c == 0]
    print(f"\nsummary coverage (saved detailed_summary):")
    print(f"  v1 names found in summary : {len(in_sum_v1)}/{len(v1_names)}  → {in_sum_v1}")
    print(f"  v1 names MISSING in summary: {miss_sum_v1}")
    print(f"  v2 names found in summary : {len(in_sum_v2)}/{len(v2_names)}  → {in_sum_v2}")
    print(f"  v2 names MISSING in summary: {miss_sum_v2}")

    return {
        "kind": kind,
        "recording_id": rec_id,
        "created_at": created,
        "transcript_chars": len(transcript),
        "summary_chars": len(summary or ""),
        "saved_in_db_cp_ids": sorted(saved_cp_ids),
        "saved_in_db_cp_names": [saved_names_by_id[i] for i in saved_cp_ids],
        "mentions": trace,
        "summary_coverage": {
            "v1_in_summary": in_sum_v1, "v1_missing_in_summary": miss_sum_v1,
            "v2_in_summary": in_sum_v2, "v2_missing_in_summary": miss_sum_v2,
        },
        "totals": {"mentions": len(trace), "agree": agree, "diff": diff, "v2_novel": v2_novel},
    }


def main() -> int:
    setup_logging()
    s = get_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=("zoom", "fireflies", "both"), default="both")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--zoom-id", help="specific ZoomRecording.zoom_id")
    g.add_argument("--fireflies-id", help="specific MeetingRecording.fireflies_id")
    g.add_argument("--latest", action="store_true", default=True,
                   help="(default) use latest of each requested source")
    ap.add_argument("--out", default="/tmp/entity_match_trace.json")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--embed-model", default=s.embedding_model)
    ap.add_argument("--extract-model", default=s.fireflies_tasks_model)
    ap.add_argument("--critic-model", default=s.entity_match_critic_model)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--skip-v1", action="store_true")
    ap.add_argument("--extract-reasoning", default="high")
    args = ap.parse_args()

    if not s.openai_api_key:
        print("ERROR: OPENAI_API_KEY not configured", file=sys.stderr)
        return 2

    client = OpenAI(api_key=s.openai_api_key)
    embed_fn = make_openai_embed_fn(client, model=args.embed_model)
    backend = OpenAIBackend(client, s.openai_model)

    sources: list[str] = []
    if args.zoom_id:
        sources = ["zoom"]
    elif args.fireflies_id:
        sources = ["fireflies"]
    else:
        sources = ["zoom", "fireflies"] if args.source == "both" else [args.source]

    full_report: list[dict] = []
    with session_scope() as session:
        directory = session.query(Counterparty).order_by(Counterparty.name).all()
        id_to_name = {c.id: c.name for c in directory}

        for kind in sources:
            if kind == "zoom":
                if args.zoom_id:
                    rec = session.query(ZoomRecording).filter(
                        ZoomRecording.zoom_id == args.zoom_id).first()
                else:
                    rec = (session.query(ZoomRecording)
                           .filter(ZoomRecording.transcript_text.isnot(None))
                           .order_by(ZoomRecording.created_at.desc()).first())
            else:
                if args.fireflies_id:
                    rec = session.query(MeetingRecording).filter(
                        MeetingRecording.fireflies_id == args.fireflies_id).first()
                else:
                    rec = (session.query(MeetingRecording)
                           .filter(MeetingRecording.transcript_text.isnot(None))
                           .order_by(MeetingRecording.created_at.desc()).first())
            if not rec:
                print(f"WARN: no {kind} recording found", file=sys.stderr)
                continue
            tr = rec.transcript_text or ""
            if not tr:
                print(f"WARN: {kind} {getattr(rec, 'zoom_id', None) or rec.fireflies_id} "
                      f"has no transcript_text", file=sys.stderr)
                continue
            full_report.append(_walk_one(
                session=session, kind=kind, rec=rec,
                transcript=tr, summary=rec.detailed_summary,
                args=args, backend=backend, embed_fn=embed_fn,
                directory=directory, id_to_name=id_to_name,
            ))

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "recordings": full_report,
        }, f, ensure_ascii=False, indent=2)
    print(f"\nfull JSON trace → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
