#!/usr/bin/env python3
"""Debug the counterparty vector matcher end-to-end.

For each mention it prints:
  1. exact lexical hit (if any) — the fast path that skips vector search;
  2. pgvector TOP-K candidates with cosine scores (`search_entities`);
  3. the critic LLM's final pick + confidence + reasoning (`match_entity`).

This is the missing observability behind «Mazon→Amazon», «Klef→Ross Cliff»
(2026-06-03): it shows whether a wrong match is a RETRIEVAL problem (right
entity not in top-K → embeddings / catalog) or a CRITIC problem (right
entity present but the LLM forced the closest one anyway → prompt/confidence).

Reads the SEPARATE pgvector catalog DB (CATALOG_DATABASE_URL). Writes nothing.

Usage:
    # explicit mentions
    docker exec -i manager-zoom-ff-1 python -m ops.debug_vector_match \\
        --mention "Mazon" --mention "Klef" --mention "Mirae"

    # pull the mentions a real run extracted, straight from its trace file
    docker exec -i manager-zoom-ff-1 python -m ops.debug_vector_match \\
        --from-trace "bqf0xAK6S8yb4Kcrzo74TA"

    # bigger K to see if the right entity is just past the cutoff
    ... --k 40
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from app.config import get_settings


def _mentions_from_trace(zoom_id: str) -> tuple[list[str], str]:
    """Read mentions (+ best-effort transcript) from a recording's main
    trace file `/app/traces/zoom-<safe_id>.jsonl`."""
    from app.services.trace_log import _safe  # type: ignore

    trace_dir = os.environ.get("MEETING_TRACE_DIR") or "/app/traces"
    fn = os.path.join(trace_dir, f"zoom-{_safe(zoom_id)}.jsonl")
    mentions: list[str] = []
    transcript = ""
    try:
        with open(fn, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    d = json.loads(ln)
                except Exception:
                    continue
                ev = d.get("event")
                flds = d.get("fields", {}) or {}
                if ev == "counterparty_extract_done" and flds.get("mentions"):
                    mentions = list(flds["mentions"])
                if ev == "counterparty_extract_call_started":
                    tp = flds.get("transcript") or flds.get("user_prompt_preview")
                    if isinstance(tp, str) and len(tp) > len(transcript):
                        transcript = tp
    except FileNotFoundError:
        print(f"ERROR: trace file not found: {fn}", file=sys.stderr)
    return mentions, transcript


def main() -> int:
    ap = argparse.ArgumentParser(description="Debug counterparty vector match")
    ap.add_argument("--mention", action="append", default=[],
                    help="Mention to resolve. Repeatable.")
    ap.add_argument("--from-trace", default=None,
                    help="zoom_id — read mentions from its trace file.")
    ap.add_argument("--transcript-file", default=None,
                    help="Optional transcript file for critic context.")
    ap.add_argument("--k", type=int, default=None,
                    help="top-K override (default: COUNTERPARTY_MATCH_V2_K).")
    ap.add_argument("--no-critic", action="store_true",
                    help="Only show vector top-K; skip the critic LLM call.")
    args = ap.parse_args()

    s = get_settings()
    mentions = list(args.mention)
    transcript = ""
    if args.from_trace:
        tm, tt = _mentions_from_trace(args.from_trace)
        mentions += tm
        transcript = tt
    if args.transcript_file:
        try:
            with open(args.transcript_file, encoding="utf-8") as f:
                transcript = f.read()
        except OSError as e:
            print(f"WARN: transcript file: {e}", file=sys.stderr)
    if not mentions:
        print("ERROR: no mentions (pass --mention or --from-trace).",
              file=sys.stderr)
        return 2

    from openai import OpenAI

    from app.db import session_scope
    from app.intent.llm_backends import OpenAIBackend
    from app.services.counterparty_catalog_resolver import open_catalog_session
    from app.services.entity_catalog import (
        CATALOG_KIND, _normalise_name, build_catalog_lexical_index,
        context_window,
    )
    from app.services.entity_embeddings import (
        make_openai_embed_fn, search_entities,
    )
    from app.services.entity_match_v2 import match_entity

    client = OpenAI(api_key=s.openai_api_key)
    embed_model = s.embedding_model
    critic_model = s.entity_match_critic_model
    embed_fn = make_openai_embed_fn(client, embed_model)
    backend = OpenAIBackend(client, critic_model)
    k = args.k or getattr(s, "counterparty_match_v2_k", 20)

    with session_scope() as fallback:
        cat, owns = open_catalog_session(fallback)
        try:
            lex, by_id = build_catalog_lexical_index(cat)
            print(f"catalog entities: {len(by_id)} | embed={embed_model} | "
                  f"critic={critic_model} | k={k}\n")

            def retrieve_fn(query: str, kk: int):
                return search_entities(
                    cat, kind=CATALOG_KIND, query_text=query,
                    embed_fn=embed_fn, model=embed_model, k=kk,
                )

            for m in mentions:
                print("=" * 72)
                print(f"MENTION: {m!r}")
                exact = lex.get(_normalise_name(m))
                if exact is not None:
                    print(f"  EXACT lexical hit → id={exact} "
                          f"name={by_id[exact].name!r}  (vector skipped)")
                cands = retrieve_fn(m, k)
                print(f"  TOP-{k} vector candidates (cosine):")
                for i, c in enumerate(cands, 1):
                    eid = c.get("entity_id")
                    nm = by_id[eid].name if eid in by_id else "?"
                    print(f"   {i:2}. {c.get('score'):.4f}  id={eid}  {nm!r}")
                if args.no_critic or exact is not None:
                    continue
                res = match_entity(
                    kind=CATALOG_KIND, mention=m,
                    context=context_window(transcript, m) if transcript else "",
                    retrieve_fn=retrieve_fn, backend=backend,
                    critic_model=critic_model, k=k,
                )
                mid = res.get("matched_entity_id")
                nm = (by_id[int(mid)].name
                      if mid and str(mid).isdigit() and int(mid) in by_id
                      else None)
                print(f"  CRITIC → matched_id={mid} name={nm!r} "
                      f"confidence={res.get('confidence')}")
                print(f"  reasoning: {res.get('reasoning')}")
        finally:
            if owns:
                cat.rollback()
                cat.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
