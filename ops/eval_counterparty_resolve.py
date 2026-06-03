#!/usr/bin/env python3
"""Precision/recall eval for counterparty resolution — directory-LLM (B)
vs vector+critic catalog (A) — on a labelled gold set.

Why: the summary canonicaliser (`resolve_organizations_to_counterparties`)
rewrites org names in the meeting summary. On the 2026-06-03 «Алина, Ирина»
run the directory-LLM force-matched phonetic garbage (Klef→Ross Cliff,
Mazon→Amazon?, K Stix→Styx). Before swapping the summary path to the
vector+critic resolver we measure both on a fixed gold set.

GOLD is editable — fix any label you disagree with, then re-run. Expected
value = prod `counterparties` canonical name, or None for «no match»
(garbage / internal person / not in directory).

Usage:
    docker exec -i manager-zoom-ff-1 python -m ops.eval_counterparty_resolve
    # with real transcript context for the critic:
    docker exec -i manager-zoom-ff-1 python -m ops.eval_counterparty_resolve \\
        --from-trace bqf0xAK6S8yb4Kcrzo74TA
"""
from __future__ import annotations

import argparse
import json
import os

# --- GOLD SET (edit freely) ------------------------------------------------ #
# mention (as extracted, raw) -> expected canonical prod name, or None.
GOLD: dict[str, str | None] = {
    # confident matches
    "Мубадала": "Mubadala",
    "аксентюр": "Accenture",
    "Тесер": "Tether",
    "виндроботикс": "WinRobotics",
    "зип": "Zip",
    "Лингота": "Lingotto",
    "к стикс": "StockX",
    "мирая": "Mirae",
    "Сива": "Siva",
    "Мазон": "Amazon",          # operator-asserted 2026-06-03
    "Химейн": "Humain",         # NB: currently suppressed by SELF_REFS filter
    # confident NON-matches (garbage / internal people / not a counterparty)
    "дев": None, "Дик": None, "Цуи": None, "Диффа": None, "Сиве": None,
    "папа": None, "Джек Ферст": None, "клеф": None, "кейти": None,
    "Триване": None, "Рейлер Лестейч": None, "Альмирая": None,
    "Киван": None, "Коше-се-е": None, "Ригби": None, "Стеф": None,
    # uncertain — adjust if you know the truth
    "труэр": None,
    "TCB": None,
}
# --------------------------------------------------------------------------- #


def _transcript_from_trace(zoom_id: str) -> str:
    from app.services.trace_log import _safe  # type: ignore
    d = os.environ.get("MEETING_TRACE_DIR") or "/app/traces"
    fn = os.path.join(d, f"zoom-{_safe(zoom_id)}.jsonl")
    best = ""
    try:
        for ln in open(fn, encoding="utf-8"):
            try:
                o = json.loads(ln)
            except Exception:
                continue
            if o.get("event") == "counterparty_extract_call_started":
                tp = (o.get("fields", {}) or {}).get("user_prompt_preview", "")
                if isinstance(tp, str) and len(tp) > len(best):
                    best = tp
    except FileNotFoundError:
        pass
    return best


def _score(name: str, gold: dict, pred: dict) -> dict:
    """pred: {mention: canonical_name | None}. Returns metrics."""
    tp = fp = fn = tn = 0
    rows = []
    for m, exp in gold.items():
        got = pred.get(m)
        # normalise comparison loosely
        ok_match = exp is not None and got is not None and \
            got.strip().lower() == exp.strip().lower()
        ok_null = exp is None and got is None
        correct = ok_match or ok_null
        if exp is not None and got is not None:
            tp += 1 if ok_match else 0
            fp += 0 if ok_match else 1   # matched, but wrong target
        elif exp is not None and got is None:
            fn += 1                       # missed a real match
        elif exp is None and got is not None:
            fp += 1                       # matched garbage
        else:
            tn += 1
        rows.append((m, exp, got, "✓" if correct else "✗"))
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    acc = sum(1 for _, _, _, v in rows if v == "✓") / len(rows) if rows else 0
    return {"name": name, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": prec, "recall": rec, "accuracy": acc, "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-trace", default=None,
                    help="zoom_id — load transcript context from its trace")
    args = ap.parse_args()

    from app.config import get_settings
    from app.db import session_scope
    from app.models.counterparty import Counterparty
    from app.services.counterparty_catalog_resolver import (
        resolve_mentions_to_directory_via_catalog,
    )
    from app.services.counterparty_match import resolve_mentions_to_directory
    from app.intent.llm_backends import OpenAIBackend
    from openai import OpenAI

    s = get_settings()
    transcript = _transcript_from_trace(args.from_trace) if args.from_trace else ""
    mentions = list(GOLD.keys())

    with session_scope() as session:
        cps = session.query(Counterparty).all()
        id_to_name = {cp.id: cp.name for cp in cps}

        # Resolver B — directory-LLM (current summary path)
        llm = OpenAIBackend(OpenAI(api_key=s.openai_api_key),
                            s.fireflies_tasks_model)
        b_ids = resolve_mentions_to_directory(
            mentions=mentions, directory=cps, llm_backend=llm,
            model=s.fireflies_tasks_model,
        )
        pred_b = {m: id_to_name.get(cid) for m, cid in b_ids.items()}

        # Resolver A — vector + critic catalog
        a_ids = resolve_mentions_to_directory_via_catalog(
            session, settings=s, mentions=mentions,
            transcript=transcript, directory=cps,
        )
        pred_a = {m: id_to_name.get(cid) for m, cid in a_ids.items()}

    rb = _score("B: directory-LLM", GOLD, pred_b)
    ra = _score("A: vector+critic", GOLD, pred_a)

    print(f"{'mention':<18} {'expected':<16} {'B→':<16} {'A→':<16}  B A")
    print("-" * 78)
    for m in mentions:
        exp = GOLD[m] or "—"
        b = pred_b.get(m) or "—"
        a = pred_a.get(m) or "—"
        bok = "✓" if (rb_row := [r for r in rb["rows"] if r[0] == m][0])[3] == "✓" else "✗"
        aok = "✓" if [r for r in ra["rows"] if r[0] == m][0][3] == "✓" else "✗"
        print(f"{m[:17]:<18} {str(exp)[:15]:<16} {str(b)[:15]:<16} {str(a)[:15]:<16}  {bok} {aok}")

    print("\n" + "=" * 50)
    for r in (rb, ra):
        print(f"{r['name']:<22} P={r['precision']:.2f} R={r['recall']:.2f} "
              f"Acc={r['accuracy']:.2f}  (tp={r['tp']} fp={r['fp']} fn={r['fn']} tn={r['tn']})")
    print("=" * 50)
    print("P=precision (из заматченного — сколько верных), "
          "R=recall (из реальных — сколько поймали), Acc=по всем меткам.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
