"""FR-CR-05-193 dry-run — прогнать V2 (Step 1 reasoning + Step 2 matcher +
Step 3 apply) для одной Zoom-записи и вывести результат БЕЗ записи в
БД, Slack или TG. Чистая diagnostic печать.

Usage:
    docker exec manager-bot-1 python -m ops.v2_dry_run_zoom \\
        --zoom-id "Nv+4Cye/TlWsngtC5ZZZiQ=="
"""
from __future__ import annotations

import argparse
import json
import sys

from openai import OpenAI

from app.config import get_settings
from app.db import session_scope
from app.intent.llm_backends import OpenAIBackend
from app.logging_setup import get_logger, setup_logging
from app.models import ZoomRecording
from app.services.entity_apply import apply_text_replacements
from app.services.entity_matcher import match_entities
from app.services.reasoning_extract import extract_summary_and_tasks
from app.services.team_members import get_humans_for_matcher
from app.services.counterparty_aliases import get_orgs_with_aliases

log = get_logger(__name__)


def main() -> int:
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--zoom-id", required=True)
    args = ap.parse_args()

    s = get_settings()
    oc = OpenAI(api_key=s.openai_api_key)
    llm = OpenAIBackend(client=oc, model=s.fireflies_tasks_model)
    model_reasoning = s.fireflies_tasks_model  # gpt-5.5
    model_matcher = s.fireflies_tasks_model

    with session_scope() as session:
        row = session.query(ZoomRecording).filter(
            ZoomRecording.zoom_id == args.zoom_id
        ).first()
        if row is None:
            print(f"ERROR: zoom_id={args.zoom_id} not found", file=sys.stderr)
            return 1
        if not (row.transcript_text or "").strip():
            print("ERROR: transcript_text empty — нужно сначала "
                  "прогнать pipeline хотя бы до transcribe step",
                  file=sys.stderr)
            return 2

        print(f"\n{'='*70}")
        print(f"V2 DRY-RUN для zoom_id={args.zoom_id}")
        print(f"title={row.title!r}, duration={row.duration_seconds}s")
        print(f"{'='*70}\n")

        # === Step 1: reasoning extract ===
        print("[Step 1/3] Reasoning LLM extract (summary + tasks)...")
        step1 = extract_summary_and_tasks(
            transcript=row.transcript_text,
            meeting_date=(row.meeting_date.isoformat()
                          if row.meeting_date else ""),
            duration_seconds=row.duration_seconds or 0,
            llm_backend=llm, model=model_reasoning,
        )
        print(f"  summary_detailed: {len(step1['summary_detailed'])} chars")
        print(f"  summary_short: {len(step1['summary_short'])} chars")
        print(f"  tasks: {len(step1['tasks'])} raw items")
        for i, t in enumerate(step1["tasks"], 1):
            print(f"    {i:2d}) raw_owner={t['raw_owner_mention']!r}  "
                  f"title={t['title'][:60]!r}")
            if t.get("description"):
                print(f"        description={t['description'][:80]!r}")
            if t.get("due_date") or t.get("priority"):
                print(f"        due={t.get('due_date')!r} "
                      f"priority={t.get('priority','medium')!r}")

        # === Step 2: matcher ===
        print(f"\n[Step 2/3] Matcher LLM (people + orgs)...")
        known_people = get_humans_for_matcher(session)
        known_orgs = get_orgs_with_aliases(session)
        print(f"  known_people: {len(known_people)} humans")
        print(f"  known_orgs: {len(known_orgs)} counterparties")

        # Объединённый текст для matcher: detailed + short + tasks titles/descriptions
        text_for_match = (step1["summary_detailed"] + "\n\n"
                          + step1["summary_short"] + "\n\n"
                          + "\n".join(
                              (t["title"] + " " + (t.get("description") or ""))
                              for t in step1["tasks"])
                          )
        raw_owners = [t["raw_owner_mention"] for t in step1["tasks"]]
        step2 = match_entities(
            text=text_for_match,
            raw_owners=raw_owners,
            known_people=known_people,
            known_orgs=known_orgs,
            llm_backend=llm, model=model_matcher,
        )
        print(f"  task_owners resolved: {len(step2['task_owners'])}")
        for o in step2["task_owners"]:
            print(f"    {o.get('raw_owner')!r:25s} → "
                  f"{o.get('tm_real_name')!r:35s}  "
                  f"({(o.get('reasoning') or '')[:50]})")
        print(f"  summary people replacements: "
              f"{len(step2.get('summary_replacements_people') or [])}")
        for r in (step2.get("summary_replacements_people") or [])[:10]:
            print(f"    {r.get('raw')!r:25s} → {r.get('canonical')!r}")
        print(f"  summary orgs replacements: "
              f"{len(step2.get('summary_replacements_orgs') or [])}")
        for r in (step2.get("summary_replacements_orgs") or [])[:10]:
            print(f"    {r.get('raw')!r:25s} → {r.get('canonical')!r}")

        # === Step 3: apply ===
        print(f"\n[Step 3/3] Deterministic apply...")
        all_repls = ((step2.get("summary_replacements_people") or [])
                     + (step2.get("summary_replacements_orgs") or []))
        final_detailed = apply_text_replacements(
            step1["summary_detailed"], replacements=all_repls,
        )
        final_short = apply_text_replacements(
            step1["summary_short"], replacements=all_repls,
        )
        # tasks final
        owner_map = {o.get("raw_owner"): o.get("tm_real_name")
                     for o in (step2.get("task_owners") or [])}
        final_tasks = []
        for t in step1["tasks"]:
            raw_o = t["raw_owner_mention"]
            canonical = owner_map.get(raw_o)
            final_tasks.append({
                **t,
                "canonical_owner": canonical,
                "title_canonical": apply_text_replacements(
                    t["title"], replacements=all_repls,
                ),
            })

        print(f"\n{'='*70}")
        print("ФИНАЛЬНЫЙ РЕЗУЛЬТАТ V2 (НЕ записано в БД, не отправлено):")
        print(f"{'='*70}")
        print(f"\n--- summary_short (canonical) ---")
        print(final_short[:1500] + ("..." if len(final_short) > 1500 else ""))
        print(f"\n--- summary_detailed первые 800 chars ---")
        print(final_detailed[:800] + ("..." if len(final_detailed) > 800
                                       else ""))
        print(f"\n--- tasks ({len(final_tasks)}) ---")
        for i, t in enumerate(final_tasks, 1):
            owner = t["canonical_owner"] or "(no_match)"
            print(f"  {i:2d}) {t['title_canonical'][:70]:70s} "
                  f"— {owner} • {t.get('priority','medium')} "
                  f"due={t.get('due_date') or '—'}")

        # Compare с legacy результатом (что в БД сейчас)
        from app.models import Task, TaskSourceKind
        legacy_tasks = session.query(Task).filter(
            Task.source_kind == TaskSourceKind.zoom,
            Task.source_conversation_id == row.zoom_id,
            Task.deleted_at.is_(None),
        ).order_by(Task.id).all()
        print(f"\n--- LEGACY result (current БД, {len(legacy_tasks)} tasks) ---")
        for i, lt in enumerate(legacy_tasks, 1):
            print(f"  {i:2d}) {(lt.title or '')[:70]:70s} "
                  f"— {lt.owner_display_name or '(none)'}")

        print(f"\n{'='*70}")
        print("V2 dry-run done. Никаких изменений в БД/Slack/TG.")
        print(f"{'='*70}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
