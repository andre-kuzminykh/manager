"""FR-CR-05-236 — LLM-assisted review of near-duplicate entity clusters.

operator-chosen path 2026-06-01 (после ручного скана первых 18 кластеров и
смешанного качества fuzzy-матчера): «LLM-ревью всех 210». Идея:

  1. `audit_entity_catalog_dupes` уже строит кластеры подозрительных
     пар (token-set + ratio). Здесь мы НЕ переописываем эту логику —
     переиспользуем `find_duplicate_candidates`.
  2. Для каждого кластера один LLM-вызов (gpt-4o, json_object) с
     полным контекстом каждой сущности: `name`, `is_org`, `parent_org`,
     `description`, `mentions_count`, `aliases`. Модель решает:
       - какие членов кластера РЕАЛЬНО та же сущность (sub-группы),
       - какой id выбрать «лидером» в каждой подгруппе (как правило,
         самый информативный / с наибольшим mentions_count),
       - confidence: high / medium / low + одна строчка-объяснение.
  3. Выход — JSON-отчёт `{"clusters":[{"members":[id...], "subgroups":
     [{"keep": id, "merge_in": [id...], "confidence": "...", "why":
     "..."}]}], "stats": {...}}`.

NO writes. Apply делается отдельным CLI `ops.merge_entities` после
ручного просмотра отчёта.

Usage:
    docker run --rm --network bridge \\
      -e DATABASE_URL="$DBURL" -e OPENAI_API_KEY=... \\
      slack-task-bot:pgv-test python -m ops.cluster_merge_review \\
      --threshold 0.86 --model gpt-4o --out /tmp/cluster_review.json \\
      [--limit-clusters 20]   # для пилотного прогона
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from openai import OpenAI

from app.config import get_settings
from app.db import session_scope
from app.intent.llm_backends import OpenAIBackend
from app.logging_setup import get_logger, setup_logging
from app.models.entity_catalog import EntityCatalogStaging
from app.services.entity_catalog import (
    DUP_RATIO_THRESHOLD,
    find_duplicate_candidates,
    load_staging_items,
)

log = get_logger(__name__)

_SYSTEM = (
    "Ты помогаешь оператору вычистить справочник контрагентов и людей "
    "от дублей. Тебе подают КЛАСТЕР сущностей, которые поверхностный "
    "матчер счёл похожими. Решай по СУТИ: одна это сущность или нет.\n\n"
    "Правила:\n"
    "• Объединяй ТОЛЬКО когда уверен, что это один и тот же реальный "
    "референт (опечатка имени, разные surface forms одной компании, "
    "юр.лицо + бренд: «LocalGlobe» ≡ «Phoenix Court (LocalGlobe)»).\n"
    "• НЕ объединяй разные продукты/фонды одного бренда: «Goldman Sachs "
    "Growth Fund» и «Goldman Sachs Asset Management» — разные сущности.\n"
    "• НЕ объединяй родительскую компанию с её инвест-арм/венчурной арм: "
    "«SoftBank» и «SoftBank Robotics», «Amazon» и «Amazon Industrial "
    "Innovation Fund» — разные.\n"
    "• НЕ объединяй однофамильцев / просто похожие по слову названия "
    "(Investcorp ≠ Supernova Invest ≠ Premji Invest).\n"
    "• Если в кластере человек и одноимённая компания — это РАЗНЫЕ "
    "сущности (Merci Grace ≠ Merci Grace Co.).\n"
    "• Если кластер реально содержит несколько разных групп — раздели на "
    "несколько subgroups; одиночек (нечего сливать) НЕ включай в "
    "subgroups вовсе.\n"
    "• Лидер (`keep`) — сущность с самым полным описанием / наибольшим "
    "mentions_count. Все остальные ids подгруппы попадают в `merge_in`.\n"
    "• `confidence`: high — точная опечатка/идентичный бренд; medium — "
    "почти наверняка одно (юр. суффиксы, региональные unitэ); low — "
    "сомнительно, оператор должен сам ревьюить.\n\n"
    "Ответ строго в JSON: {\"subgroups\":[{\"keep\": <id>, \"merge_in\": "
    "[<id>,...], \"confidence\": \"high|medium|low\", \"why\": "
    "\"<краткая причина>\"}, ...]}. Если в кластере нет ни одной "
    "реальной группы для мёржа — верни {\"subgroups\":[]}."
)


def _enrich(session, ids: list[int]) -> list[dict[str, Any]]:
    """Pull description / parent_org / mentions_count / aliases for the
    cluster members so the LLM has the full picture."""
    rows = (
        session.query(EntityCatalogStaging)
        .filter(EntityCatalogStaging.id.in_(ids))
        .all()
    )
    return [
        {
            "id": r.id, "name": r.name, "is_org": bool(r.is_org),
            "parent_org": r.parent_org or "",
            "mentions_count": r.mentions_count or 0,
            "aliases": (r.aliases or "")[:300],
            "description": (r.description or "")[:500],
        }
        for r in rows
    ]


def _review_cluster(llm: OpenAIBackend, model: str, members: list[dict]) -> dict:
    payload = {"cluster": members}
    user_prompt = (
        f"Кластер кандидатов на дубль ({len(members)} entries):\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
    resp = llm.complete_text(
        system_prompt=_SYSTEM,
        user_prompt=user_prompt,
        model=model,
        response_format={"type": "json_object"},
    )
    if not resp:
        return {"subgroups": [], "_error": "llm_empty"}
    try:
        out = json.loads(resp)
    except json.JSONDecodeError as e:
        return {"subgroups": [], "_error": f"json_decode: {e}"}
    sgs = out.get("subgroups") or []
    valid_ids = {m["id"] for m in members}
    cleaned: list[dict] = []
    for sg in sgs:
        keep = sg.get("keep")
        merge_in = [i for i in (sg.get("merge_in") or []) if i in valid_ids and i != keep]
        if keep not in valid_ids or not merge_in:
            continue
        cleaned.append({
            "keep": keep,
            "merge_in": merge_in,
            "confidence": (sg.get("confidence") or "low").lower(),
            "why": (sg.get("why") or "").strip()[:300],
        })
    return {"subgroups": cleaned}


def main() -> int:
    setup_logging()
    s = get_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=DUP_RATIO_THRESHOLD)
    ap.add_argument("--model", default="gpt-4o")
    ap.add_argument("--out", default="/tmp/cluster_review.json")
    ap.add_argument("--limit-clusters", type=int, default=0,
                    help="0 = все; иначе только первые N (для пилота)")
    ap.add_argument("--sleep", type=float, default=0.2)
    args = ap.parse_args()

    if not s.openai_api_key:
        print("ERROR: OPENAI_API_KEY not set", file=sys.stderr)
        return 2

    llm = OpenAIBackend(OpenAI(api_key=s.openai_api_key), args.model)

    with session_scope() as session:
        items = load_staging_items(session)
        clusters = find_duplicate_candidates(items, threshold=args.threshold)
        if args.limit_clusters:
            clusters = clusters[: args.limit_clusters]
        log.info("cluster_review_start", total_clusters=len(clusters),
                 threshold=args.threshold, model=args.model)

        report = {
            "clusters": [],
            "stats": {
                "total_clusters": len(clusters),
                "threshold": args.threshold, "model": args.model,
            },
        }
        for i, cl in enumerate(clusters, 1):
            ids = [m["id"] for m in cl]
            enriched = _enrich(session, ids)
            try:
                verdict = _review_cluster(llm, args.model, enriched)
            except Exception as e:  # noqa: BLE001
                log.warning("cluster_review_failed", i=i, ids=ids, err=str(e))
                verdict = {"subgroups": [], "_error": str(e)}
            entry = {
                "members": [
                    {"id": m["id"], "name": m["name"], "is_org": m["is_org"]}
                    for m in enriched
                ],
                "verdict": verdict,
            }
            report["clusters"].append(entry)
            if i % 10 == 0:
                log.info("cluster_review_progress", done=i, total=len(clusters))
            time.sleep(max(0.0, args.sleep))
        session.rollback()  # READ-ONLY

    total_pairs = sum(
        len(sg["merge_in"])
        for c in report["clusters"]
        for sg in c["verdict"].get("subgroups", [])
    )
    by_conf = {"high": 0, "medium": 0, "low": 0}
    for c in report["clusters"]:
        for sg in c["verdict"].get("subgroups", []):
            by_conf[sg["confidence"]] = by_conf.get(sg["confidence"], 0) + 1
    report["stats"]["proposed_merges"] = total_pairs
    report["stats"]["subgroups_by_confidence"] = by_conf

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(f"[ok] clusters={len(report['clusters'])}, "
          f"предложено мёржей={total_pairs}, "
          f"по уверенности={by_conf}\n  отчёт: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
