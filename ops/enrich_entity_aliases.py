"""FR-CR-05-238 — enrich `entity_catalog_staging.aliases` with common
abbreviations / acronyms / short forms so vector search recalls terse
mentions («GS PWM» → Goldman Sachs Private Wealth Management) that raw
name embeddings miss (operator test 2026-06-01).

LLM batch pass (gpt-4o, json_object): for a batch of entities it returns
SAFE aliases only — abbreviations/acronyms people actually use, obvious
short forms, and well-known alternate spellings (incl. a Cyrillic form
for global brands like Hyundai→Хёндай). Conservative: no guesses, never
repeats the canonical name.

Merge contract: aliases ← union(existing, llm_aliases), case-insensitive
dedup, drop any equal to the entity name. NEVER removes existing aliases.

SAFE: dry-run default, --apply explicit, --backup JSON of touched rows,
--kind org|person|all (default org — that's where the recall gap is),
--limit / --min-mentions for piloting.

Usage:
    docker run --rm --network bridge -e DATABASE_URL="$DBURL" \\
      -e OPENAI_API_KEY=... slack-task-bot:pgv-test \\
      python -m ops.enrich_entity_aliases --kind org --limit 40   # dry-run
    # ...then --apply ; then re-run ops.catalog_vector_search embed
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from openai import OpenAI
from sqlalchemy import select, text as sql_text

from app.config import get_settings
from app.db import session_scope
from app.logging_setup import get_logger, setup_logging
from app.models.entity_catalog import EntityCatalogStaging

log = get_logger(__name__)

_SYSTEM = (
    "Ты пополняешь справочник сущностей АЛИАСАМИ для поиска. На вход — "
    "список организаций/людей (id, name, description). Для каждого верни "
    "список РЕАЛЬНО используемых коротких форм, по которым их называют:\n"
    "  • аббревиатуры и акронимы: «Goldman Sachs Private Wealth Management» "
    "→ [\"GS PWM\", \"GSPWM\", \"GS\"]; «Bank of America» → [\"BofA\", \"BoA\"]; "
    "«Hong Kong Investment Corporation» → [\"HKIC\"];\n"
    "  • очевидные короткие формы: «Tiger Global Management» → [\"Tiger "
    "Global\"]; «Andreessen Horowitz» → [\"a16z\"];\n"
    "  • широко известное альт-написание, в т.ч. кириллицей для глобальных "
    "брендов: «Hyundai» → [\"Хёндай\"]; «SoftBank» → [\"Софтбанк\"].\n\n"
    "ПРАВИЛА:\n"
    "  – ТОЛЬКО то, что реально употребляют. НЕ выдумывай. Сомневаешься — "
    "не добавляй (пустой список).\n"
    "  – НЕ повторяй само каноническое имя.\n"
    "  – НЕ давай чужие/родственные имена (не добавляй «Goldman Sachs» "
    "для «Goldman Sachs Growth Fund» — это другая сущность).\n"
    "  – Максимум 6 алиасов на сущность.\n\n"
    "Ответ строго JSON: {\"results\":[{\"id\": <int>, \"aliases\": "
    "[\"...\"]}, ...]}. Сущности без надёжных алиасов можно опустить или "
    "вернуть с пустым списком."
)


def _alias_union(existing: str | None, additions: list[str], *, name: str) -> str | None:
    """union(existing, additions), case-insensitive dedup, drop blanks and
    anything equal to the entity name. Stable order: existing first."""
    out: list[str] = []
    seen: set[str] = set()
    name_k = (name or "").strip().lower()
    def _add(a: str) -> None:
        a = (a or "").strip()
        if not a:
            return
        k = a.lower()
        if k == name_k or k in seen:
            return
        seen.add(k)
        out.append(a)
    for a in (existing or "").split(","):
        _add(a)
    for a in additions:
        _add(a)
    return ", ".join(out) if out else (existing or None)


def _llm_aliases(llm, model: str, batch: list[EntityCatalogStaging]) -> dict[int, list[str]]:
    payload = [
        {"id": r.id, "name": r.name,
         "description": " ".join((r.description or "").split())[:200]}
        for r in batch
    ]
    resp = llm.complete_text(
        system_prompt=_SYSTEM,
        user_prompt="Сущности:\n" + json.dumps(payload, ensure_ascii=False),
        model=model, response_format={"type": "json_object"},
    )
    if not resp:
        return {}
    try:
        data = json.loads(resp)
    except json.JSONDecodeError:
        return {}
    out: dict[int, list[str]] = {}
    valid = {r.id for r in batch}
    for item in data.get("results", []):
        rid = item.get("id")
        al = item.get("aliases") or []
        if isinstance(rid, int) and rid in valid and isinstance(al, list):
            out[rid] = [str(a) for a in al if str(a).strip()]
    return out


def main() -> int:
    setup_logging()
    s = get_settings()
    from app.intent.llm_backends import OpenAIBackend

    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["org", "person", "all"], default="org")
    ap.add_argument("--model", default="gpt-4o")
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--limit", type=int, default=0, help="0 = все")
    ap.add_argument("--min-mentions", type=int, default=0)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup", default=f"/tmp/aliases_backup_{int(time.time())}.json")
    ap.add_argument("--sleep", type=float, default=0.2)
    args = ap.parse_args()

    if not s.openai_api_key:
        print("ERROR: OPENAI_API_KEY not set", file=sys.stderr)
        return 2
    llm = OpenAIBackend(OpenAI(api_key=s.openai_api_key), args.model)

    with session_scope() as session:
        stmt = select(EntityCatalogStaging)
        if args.kind == "org":
            stmt = stmt.where(EntityCatalogStaging.is_org.is_(True))
        elif args.kind == "person":
            stmt = stmt.where(EntityCatalogStaging.is_org.is_(False))
        if args.min_mentions:
            stmt = stmt.where(EntityCatalogStaging.mentions_count >= args.min_mentions)
        stmt = stmt.order_by(EntityCatalogStaging.mentions_count.desc(), EntityCatalogStaging.id)
        rows = session.execute(stmt).scalars().all()
        if args.limit:
            rows = rows[: args.limit]
        print(f"кандидатов ({args.kind}): {len(rows)}  apply={args.apply}")

        if args.apply:
            ids = [r.id for r in rows]
            snap = [
                {"id": r.id, "name": r.name, "aliases": r.aliases}
                for r in session.execute(
                    select(EntityCatalogStaging).where(EntityCatalogStaging.id.in_(ids))
                ).scalars().all()
            ]
            with open(args.backup, "w", encoding="utf-8") as fh:
                json.dump({"ts": time.time(), "rows": snap}, fh, ensure_ascii=False, indent=2)
            print(f"backup: {args.backup}")

        changed = 0
        shown = 0
        for i in range(0, len(rows), args.batch):
            chunk = rows[i:i + args.batch]
            try:
                amap = _llm_aliases(llm, args.model, chunk)
            except Exception as e:  # noqa: BLE001
                log.warning("alias_llm_failed", i=i, err=str(e)); continue
            for r in chunk:
                adds = amap.get(r.id, [])
                if not adds:
                    continue
                new = _alias_union(r.aliases, adds, name=r.name)
                if new != (r.aliases or None):
                    if shown < 60:
                        print(f"  {r.id} «{r.name}»: +{adds}  →  {new}")
                        shown += 1
                    if args.apply:
                        r.aliases = new
                    changed += 1
            if args.apply:
                session.commit()
            time.sleep(max(0.0, args.sleep))

        if args.apply:
            print(f"\n[apply] обновлено сущностей: {changed}")
        else:
            session.rollback()
            print(f"\n[dry-run] изменилось бы: {changed} (ничего не записано)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
