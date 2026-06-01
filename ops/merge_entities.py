"""FR-CR-05-236 — safely merge near-duplicate entities in the staging
catalog. Applies the verdicts from `ops.cluster_merge_review` (или явные
пары `--from N --into M`) к `entity_catalog_staging`.

CONTRACT (это и есть спека для тестов):
  Merge(from=F, into=T):
    * T.aliases  ← union(T.aliases, F.aliases) ∪ {F.name if F.name != T.name}
      (запятой-разделённый список, без дублей, порядок устойчивый).
    * T.description ← if T.description ⊇ F.description → unchanged;
      else склейка "T.desc\\n\\nF.desc" (с дедупом, обрезка по DESCRIPTION_LIMIT).
    * T.mentions_count ← T.mentions_count + F.mentions_count.
    * T.parent_org ← T.parent_org or F.parent_org   (fill-if-empty).
    * T.is_org оставляем как у T (лидер выбирается заранее).
    * Удаляем строку F.  Возможный конфликт UNIQUE(name_normalised,
      is_org) на T — невозможен (T уже единственный с таким ключом).

SAFETY:
  * dry-run по умолчанию. --apply обязательно явно.
  * перед каждым apply пишем JSON-дамп ВСЕХ изменяемых/удаляемых строк
    в --backup PATH (по умолчанию /tmp/merge_backup_<ts>.json) → ровный
    rollback.
  * --confidence min=high|medium|low — фильтр по уровню уверенности из
    LLM-отчёта (default: high). Низкоуверенные не применяем автоматом.
  * --limit N — ограничение количества мёржей за прогон (для пилота).

Usage:
    # из LLM-отчёта (high-confidence only):
    docker run --rm --network bridge -e DATABASE_URL="$DBURL" \\
      slack-task-bot:pgv-test python -m ops.merge_entities \\
      --review /tmp/cluster_review.json --confidence high \\
      --backup /tmp/merge_high.json    # dry-run
    # ...затем то же + --apply
    # явная одиночная пара:
    docker run ... python -m ops.merge_entities --from 1141 --into 1103 --apply
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import session_scope
from app.logging_setup import get_logger, setup_logging
from app.models.entity_catalog import EntityCatalogStaging
from app.services.entity_catalog import DESCRIPTION_LIMIT, merge_description

log = get_logger(__name__)

_CONF_ORDER = {"high": 3, "medium": 2, "low": 1}


def _alias_set(s: str | None) -> list[str]:
    """Comma-separated → ordered unique list, trimmed."""
    if not s:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for a in s.split(","):
        a = a.strip()
        if a and a.lower() not in seen:
            seen.add(a.lower())
            out.append(a)
    return out


def merge_pair(
    session: Session, *, src: EntityCatalogStaging, dst: EntityCatalogStaging,
) -> dict[str, Any]:
    """Apply the merge contract in-place on `dst`, delete `src`. Returns
    a diff dict for logging."""
    before = {
        "dst_aliases": dst.aliases, "dst_desc_len": len(dst.description or ""),
        "dst_mentions": dst.mentions_count, "dst_parent": dst.parent_org,
    }
    # aliases: union of (dst aliases, src aliases, src.name if differs)
    merged_aliases = _alias_set(dst.aliases) + _alias_set(src.aliases)
    if src.name and src.name.strip().lower() != (dst.name or "").strip().lower():
        merged_aliases.append(src.name.strip())
    seen: set[str] = set()
    deduped: list[str] = []
    for a in merged_aliases:
        k = a.lower()
        if k in seen:
            continue
        seen.add(k)
        deduped.append(a)
    dst.aliases = ", ".join(deduped) if deduped else None
    # description: merge with the existing helper (handles dedup + limit)
    dst.description = merge_description(
        dst.description or "", src.description or "", limit=DESCRIPTION_LIMIT,
    )
    # mentions_count: sum
    dst.mentions_count = (dst.mentions_count or 0) + (src.mentions_count or 0)
    # parent_org: fill if empty
    if not (dst.parent_org or "").strip() and (src.parent_org or "").strip():
        dst.parent_org = src.parent_org.strip()
    session.delete(src)
    session.flush()
    return {
        "from_id": src.id, "into_id": dst.id,
        "before": before,
        "after": {
            "dst_aliases": dst.aliases, "dst_desc_len": len(dst.description or ""),
            "dst_mentions": dst.mentions_count, "dst_parent": dst.parent_org,
        },
    }


def _collect_pairs(args, exclude_src: set[int] | None = None) -> list[tuple[int, int, str, str]]:
    """Return [(from_id, into_id, confidence, why)] from --review or
    explicit --from/--into. Pairs whose source id ∈ exclude_src are
    dropped (operator review found them unsafe).

    `--only-confidence X` selects EXACTLY level X (e.g. just 'medium'
    after 'high' is already applied); otherwise `--confidence X` is a
    floor (X and above)."""
    exclude_src = exclude_src or set()
    only = getattr(args, "only_confidence", None)
    pairs: list[tuple[int, int, str, str]] = []
    if args.from_id and args.into_id:
        pairs.append((int(args.from_id), int(args.into_id), "manual", "explicit pair"))
        return [p for p in pairs if p[0] not in exclude_src]
    if not args.review:
        return pairs
    with open(args.review, encoding="utf-8") as fh:
        report = json.load(fh)
    min_conf = _CONF_ORDER[args.confidence]
    for cl in report.get("clusters", []):
        for sg in (cl.get("verdict") or {}).get("subgroups", []):
            conf = sg.get("confidence", "low")
            if only:
                if conf != only:
                    continue
            elif _CONF_ORDER.get(conf, 0) < min_conf:
                continue
            keep = sg.get("keep")
            for src in sg.get("merge_in", []):
                if isinstance(keep, int) and isinstance(src, int) and keep != src:
                    if src in exclude_src or keep in exclude_src:
                        continue
                    pairs.append((src, keep, conf, sg.get("why", "")))
    return pairs


def _dump_backup(session: Session, ids: set[int], path: str) -> None:
    rows = session.execute(
        select(EntityCatalogStaging).where(EntityCatalogStaging.id.in_(ids))
    ).scalars().all()
    snap = [
        {
            "id": r.id, "name": r.name, "name_normalised": r.name_normalised,
            "is_org": r.is_org, "parent_org": r.parent_org,
            "description": r.description, "aliases": r.aliases,
            "mentions_count": r.mentions_count,
        }
        for r in rows
    ]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"ts": time.time(), "rows": snap}, fh,
                  ensure_ascii=False, indent=2)


def main() -> int:
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--review", default=None, help="JSON from cluster_merge_review")
    ap.add_argument("--from", dest="from_id", type=int, default=0)
    ap.add_argument("--into", dest="into_id", type=int, default=0)
    ap.add_argument("--confidence", choices=["high", "medium", "low"],
                    default="high",
                    help="минимальный уровень из LLM-отчёта (floor; default: high)")
    ap.add_argument("--only-confidence", choices=["high", "medium", "low"],
                    default=None,
                    help="ровно ОДИН уровень (e.g. medium после применённого high)")
    ap.add_argument("--limit", type=int, default=0, help="0 = без лимита")
    ap.add_argument("--apply", action="store_true",
                    help="ЗАПИСАТЬ изменения (default: dry-run)")
    ap.add_argument("--backup", default=f"/tmp/merge_backup_{int(time.time())}.json")
    ap.add_argument("--exclude", type=int, nargs="*", default=[],
                    help="entity ids to SKIP (review found them unsafe); "
                         "skips any pair where this id is source OR leader")
    ap.add_argument("--allow-cross-isorg", action="store_true",
                    help="разрешить мёрж person↔org (по умолчанию запрещён). "
                         "Только для ЯВНЫХ ручных пар (--from/--into), когда "
                         "оператор подтвердил, что это один референт "
                         "(напр. человек, ошибочно заведённый и как org).")
    args = ap.parse_args()

    pairs = _collect_pairs(args, exclude_src=set(args.exclude or []))
    if args.limit:
        pairs = pairs[: args.limit]
    if not pairs:
        print("nothing to merge (no --review / --from --into or filter wiped all).",
              file=sys.stderr)
        return 1

    _tier = (f"only={args.only_confidence}" if args.only_confidence
             else f"min_conf={args.confidence}")
    print(f"запланировано мёржей: {len(pairs)} (apply={args.apply}, {_tier})")

    with session_scope() as session:
        ids = {p[0] for p in pairs} | {p[1] for p in pairs}
        rows = session.execute(
            select(EntityCatalogStaging).where(EntityCatalogStaging.id.in_(ids))
        ).scalars().all()
        by_id = {r.id: r for r in rows}
        missing = [i for i in ids if i not in by_id]
        if missing:
            print(f"WARN: id не найдены в БД: {missing[:20]}", file=sys.stderr)

        if args.apply:
            _dump_backup(session, ids, args.backup)
            print(f"backup сохранён: {args.backup}")

        applied = skipped = 0
        for src_id, dst_id, conf, why in pairs:
            src = by_id.get(src_id); dst = by_id.get(dst_id)
            if src is None or dst is None or src.id == dst.id:
                skipped += 1; continue
            if src.is_org != dst.is_org and not args.allow_cross_isorg:
                print(f"  SKIP {src_id}→{dst_id}: is_org разные ({src.is_org} vs {dst.is_org}) — мёрж per↔org не делаем автоматом ({why})")
                skipped += 1; continue
            print(f"  {('APPLY' if args.apply else 'DRY  ')} "
                  f"[{conf}] {src.id} «{src.name}» → {dst.id} «{dst.name}»  "
                  f"({why})")
            if args.apply:
                merge_pair(session, src=src, dst=dst)
                applied += 1
        if args.apply:
            session.commit()
            print(f"\nприменено: {applied}, пропущено: {skipped}")
        else:
            session.rollback()
            print(f"\nDRY-RUN: ничего не записано. К записи готово: {len(pairs)-skipped}, пропущено: {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
