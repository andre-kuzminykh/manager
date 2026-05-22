"""FR-CR-05-193f — one-shot op для backfill aliases для counterparties.

Batch LLM call (50 per call). Идемпотентен — skip если aliases уже есть.

Usage:
    docker exec manager-bot-1 python -m ops.backfill_counterparty_aliases \\
        [--dry-run] [--batch-size 50]
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from app.config import get_settings
from app.db import session_scope
from app.intent.llm_backends import OpenAIBackend
from app.logging_setup import get_logger, setup_logging
from app.services.counterparty_aliases import (
    ALIASES_SOURCE, save_aliases,
)
from openai import OpenAI
from sqlalchemy import text as sql_text

log = get_logger(__name__)


_BACKFILL_PROMPT = """Ты эксперт по нейминг'у инвестфондов и компаний.
Для каждой компании из списка ниже дай 1-3 canonical-варианта (русская
транслитерация, краткие/полные формы, известные псевдонимы). НЕ инвентируй
несуществующих имён.

Примеры:
  Schaeffler → ["Шеффлер","Шафлер","Schaeffler AG"]
  Bain Capital → ["Bain","Бэйн","Bain Cap"]
  KKR → ["Kohlberg Kravis Roberts","ККР"]

Список:
{listing}

Верни СТРОГО JSON формат:
{{
  "aliases_by_id": {{
    "<id>": ["alias1", "alias2", ...]
  }}
}}
"""


def backfill_aliases(
    *,
    session: Any,
    llm_backend: Any,
    model: str,
    dry_run: bool = False,
    batch_size: int = 50,
) -> dict:
    """Returns stats: {total, skipped, processed, llm_calls}."""
    from app.models import Counterparty

    # Все counterparties
    all_cps = list(session.query(Counterparty).all())
    if not all_cps:
        return {"total": 0, "skipped": 0, "processed": 0, "llm_calls": 0}

    # Уже с aliases — skip
    existing_rows = session.execute(
        sql_text(
            "SELECT counterparty_id FROM counterparty_attrs WHERE source = :s"
        ),
        {"s": ALIASES_SOURCE},
    ).all()
    existing_ids = {r[0] for r in existing_rows}

    todo = [c for c in all_cps if c.id not in existing_ids]
    stats = {
        "total": len(all_cps),
        "skipped": len(all_cps) - len(todo),
        "processed": 0,
        "llm_calls": 0,
    }

    if not todo:
        log.info("backfill_aliases_done_all_skipped", **stats)
        return stats

    # Batch
    for i in range(0, len(todo), batch_size):
        batch = todo[i : i + batch_size]
        listing = "\n".join(f"  {c.id}: {c.name}" for c in batch)
        prompt = _BACKFILL_PROMPT.format(listing=listing)
        try:
            raw = llm_backend.complete_text(
                system_prompt="You are a fund/company naming expert.",
                user_prompt=prompt,
                model=model,
                reasoning_effort="low",
            )
            stats["llm_calls"] += 1
        except Exception as e:  # noqa: BLE001
            log.warning("backfill_llm_error", error=str(e))
            continue

        try:
            text = str(raw).strip()
            if text.startswith("```"):
                text = text.lstrip("`").lstrip("json").strip()
                if text.endswith("```"):
                    text = text[:-3].strip()
            parsed = json.loads(text)
            aliases_by_id = parsed.get("aliases_by_id") or {}
        except (json.JSONDecodeError, AttributeError) as e:
            log.warning("backfill_json_error", error=str(e))
            continue

        for cp in batch:
            aliases = aliases_by_id.get(str(cp.id)) or []
            if not aliases:
                continue
            if dry_run:
                print(f"[DRY] cp={cp.id} «{cp.name}» → aliases={aliases}")
            else:
                save_aliases(session,
                             counterparty_id=cp.id, aliases=aliases)
            stats["processed"] += 1

    if not dry_run:
        session.commit()

    log.info("backfill_aliases_done", **stats)
    return stats


def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=50)
    args = parser.parse_args()

    s = get_settings()
    llm = OpenAIBackend(
        OpenAI(api_key=s.openai_api_key),
        s.fireflies_tasks_model,
    )

    with session_scope() as session:
        stats = backfill_aliases(
            session=session, llm_backend=llm,
            model=s.fireflies_tasks_model,
            dry_run=args.dry_run,
            batch_size=args.batch_size,
        )
    print(f"\nBackfill done: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
