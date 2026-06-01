"""FR-CR-05-241 — Phase B of the OFFLINE shadow: resolve the dumped prod
meetings through v2 (catalog hybrid: exact alias → pgvector → critic) and
compare to v1 (prod whole-directory resolve). READ-ONLY; prints a report
+ optional JSON. Validates v2 on REAL meetings with ZERO prod touch.

Comparison is by NORMALISED NAME (v1 ids are prod-directory ids, v2 ids
are catalog ids — different directories), so we compare which real
entities each method surfaced:
  - both      : v1 and v2 agree on an entity
  - v1_only   : v1 surfaced it, v2 didn't (real miss OR v1 garbage-enroll)
  - v2_only   : v2 surfaced it, v1 didn't
Also reports v2 latency per meeting (vs v1's ~8.85 min directory-LLM).

Run in the pgvector sidecar:
    docker run --rm --network bridge --env-file ~/manager/.env \\
      -e DATABASE_URL="$DBURL" -e OPENAI_API_KEY=... -v /tmp:/host \\
      slack-task-bot:pgv-test python -m ops.shadow_report \\
      --in /host/shadow_prod.json --out /host/shadow_report.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from openai import OpenAI

from app.config import get_settings
from app.db import session_scope
from app.intent.llm_backends import OpenAIBackend
from app.logging_setup import get_logger, setup_logging
from app.services.counterparty_match import extract_counterparty_mentions
from app.services.entity_catalog import _normalise_name
from app.services.entity_embeddings import make_openai_embed_fn, search_entities
from app.services.entity_match_v2 import match_entity
from ops.catalog_resolve import KIND, _build_lexical_index, _context_for

log = get_logger(__name__)


def _resolve_transcript(session, transcript, *, lex, by_id, embed_fn, backend,
                        extract_model, critic_model, k):
    """Return (mentions, {entity_id: name}, method_counts). Hybrid v2."""
    mentions = extract_counterparty_mentions(
        transcript, llm_backend=backend, model=extract_model, reasoning_effort="high",
    )

    def retrieve_fn(query, kk):
        return search_entities(session, kind=KIND, query_text=query,
                               embed_fn=embed_fn,
                               model="text-embedding-3-large", k=kk)

    matched: dict[int, str] = {}
    n_exact = n_critic = n_none = 0
    for m in mentions:
        eid = lex.get(_normalise_name(m))
        if eid is not None:
            matched[eid] = by_id[eid].name
            n_exact += 1
            continue
        res = match_entity(kind=KIND, mention=m, context=_context_for(transcript, m),
                           retrieve_fn=retrieve_fn, backend=backend,
                           critic_model=critic_model, k=k)
        mid = res.get("matched_entity_id")
        if mid and str(mid).isdigit() and int(mid) in by_id:
            matched[int(mid)] = by_id[int(mid)].name
            n_critic += 1
        else:
            n_none += 1
    return mentions, matched, {"exact": n_exact, "critic": n_critic, "none": n_none}


def main() -> int:
    setup_logging()
    s = get_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", required=True)
    ap.add_argument("--out", default="/tmp/shadow_report.json")
    ap.add_argument("--extract-model", default=s.openai_model)
    ap.add_argument("--critic-model", default=getattr(s, "entity_match_critic_model", "gpt-4o"))
    ap.add_argument("--k", type=int, default=12)
    args = ap.parse_args()

    if not s.openai_api_key:
        print("ERROR: OPENAI_API_KEY not set", file=sys.stderr)
        return 2
    client = OpenAI(api_key=s.openai_api_key)
    backend = OpenAIBackend(client, args.extract_model)
    embed_fn = make_openai_embed_fn(client, "text-embedding-3-large")

    with open(args.infile, encoding="utf-8") as fh:
        meetings = json.load(fh).get("meetings", [])
    print(f"встреч на сверку: {len(meetings)} (k={args.k})\n")

    with session_scope() as session:
        lex, by_id = _build_lexical_index(session)
        report = []
        agg = {"both": 0, "v1_only": 0, "v2_only": 0,
               "exact": 0, "critic": 0, "none": 0, "v2_seconds": 0.0}
        for mt in meetings:
            t0 = time.time()
            mentions, matched, mc = _resolve_transcript(
                session, mt["transcript"], lex=lex, by_id=by_id,
                embed_fn=embed_fn, backend=backend,
                extract_model=args.extract_model, critic_model=args.critic_model, k=args.k)
            secs = round(time.time() - t0, 1)

            v1n = {_normalise_name(n): n for n in mt["v1_counterparties"]}
            v2n = {_normalise_name(n): n for n in matched.values()}
            both = sorted(v1n[k] for k in v1n.keys() & v2n.keys())
            v1_only = sorted(v1n[k] for k in v1n.keys() - v2n.keys())
            v2_only = sorted(v2n[k] for k in v2n.keys() - v1n.keys())

            for key, n in mc.items():
                agg[key] += n
            agg["both"] += len(both); agg["v1_only"] += len(v1_only)
            agg["v2_only"] += len(v2_only); agg["v2_seconds"] += secs

            print(f"=== {mt['title'][:44]} | {secs}s | mentions={len(mentions)} "
                  f"(exact={mc['exact']} critic={mc['critic']} none={mc['none']}) ===")
            print(f"   both({len(both)}): {', '.join(both)[:120]}")
            print(f"   v1_only({len(v1_only)}): {', '.join(v1_only)[:120]}")
            print(f"   v2_only({len(v2_only)}): {', '.join(v2_only)[:120]}\n")
            report.append({"title": mt["title"], "seconds": secs,
                           "method": mc, "both": both,
                           "v1_only": v1_only, "v2_only": v2_only})
        session.rollback()

    avg = round(agg["v2_seconds"] / max(1, len(meetings)), 1)
    print("================ ИТОГО ================")
    print(f"встреч: {len(meetings)} | v2 ср. время/встреча: {avg}s "
          f"(v1 ~8.85 мин/встреча)")
    print(f"совпали (both): {agg['both']} | только v1: {agg['v1_only']} "
          f"(часть — мусорный энролл) | только v2: {agg['v2_only']}")
    print(f"v2 методы: exact={agg['exact']} critic={agg['critic']} none={agg['none']}")
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"aggregate": agg, "meetings": report}, fh, ensure_ascii=False, indent=2)
    print(f"отчёт: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
