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

        # Meeting participants — для SPEAKER FALLBACK + STRICT rule
        # task_owners только из этих имён.
        meeting_participants: list[str] = []
        if row.calendar_attendees:
            try:
                for a in row.calendar_attendees:
                    if isinstance(a, dict):
                        name = (a.get("resolved_name")
                                or a.get("display_name")
                                or "").strip()
                        if name and name not in meeting_participants:
                            meeting_participants.append(name)
            except Exception:  # noqa: BLE001
                pass
        if not meeting_participants and row.participants:
            try:
                for p in row.participants:
                    s = str(p).strip()
                    if s and s not in meeting_participants:
                        meeting_participants.append(s)
            except Exception:  # noqa: BLE001
                pass
        print(f"  meeting_participants: {len(meeting_participants)} → "
              f"{meeting_participants}")

        step2 = match_entities(
            text=text_for_match,
            raw_owners=raw_owners,
            known_people=known_people,
            known_orgs=known_orgs,
            meeting_participants=meeting_participants,
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

        # === Step 3: apply with detailed trace ===
        print(f"\n[Step 3/3] Deterministic apply WITH FULL TRACE...")
        people_repls = step2.get("summary_replacements_people") or []
        orgs_repls = step2.get("summary_replacements_orgs") or []
        all_repls = people_repls + orgs_repls

        # Per-replacement trace: count occurrences в каждом фрагменте
        print(f"\n  PEOPLE replacements ({len(people_repls)}):")
        for r in people_repls:
            raw, canon = r.get("raw") or "", r.get("canonical") or ""
            n_detailed = step1["summary_detailed"].count(raw)
            n_short = step1["summary_short"].count(raw)
            n_tasks = sum(
                (t["title"] + " " + (t.get("description") or "")).count(raw)
                for t in step1["tasks"]
            )
            print(f"    «{raw}» → «{canon}»  "
                  f"detailed={n_detailed}× short={n_short}× tasks={n_tasks}×")

        print(f"\n  ORGS replacements ({len(orgs_repls)}):")
        for r in orgs_repls:
            raw, canon = r.get("raw") or "", r.get("canonical") or ""
            n_detailed = step1["summary_detailed"].count(raw)
            n_short = step1["summary_short"].count(raw)
            n_tasks = sum(
                (t["title"] + " " + (t.get("description") or "")).count(raw)
                for t in step1["tasks"]
            )
            print(f"    «{raw}» → «{canon}»  "
                  f"detailed={n_detailed}× short={n_short}× tasks={n_tasks}×")

        # Apply
        final_detailed = apply_text_replacements(
            step1["summary_detailed"], replacements=all_repls,
        )
        final_short = apply_text_replacements(
            step1["summary_short"], replacements=all_repls,
        )

        # tasks final
        owner_map = {o.get("raw_owner"): o.get("tm_real_name")
                     for o in (step2.get("task_owners") or [])}
        owner_reasoning_map = {o.get("raw_owner"): o.get("reasoning") or ""
                               for o in (step2.get("task_owners") or [])}
        final_tasks = []
        for t in step1["tasks"]:
            raw_o = t["raw_owner_mention"]
            canonical = owner_map.get(raw_o)
            final_tasks.append({
                **t,
                "canonical_owner": canonical,
                "owner_reasoning": owner_reasoning_map.get(raw_o, ""),
                "title_canonical": apply_text_replacements(
                    t["title"], replacements=all_repls,
                ),
                "description_canonical": apply_text_replacements(
                    t.get("description") or "", replacements=all_repls,
                ),
            })

        # Diff trace для каждого изменения в short_summary
        if step1["summary_short"] != final_short:
            print(f"\n  short_summary diff (line-by-line changes):")
            old_lines = step1["summary_short"].split("\n")
            new_lines = final_short.split("\n")
            for i, (old, new) in enumerate(zip(old_lines, new_lines)):
                if old != new:
                    print(f"    L{i+1}: «{old[:80]}»")
                    print(f"      → «{new[:80]}»")

        print(f"\n{'='*70}")
        print("ФИНАЛЬНЫЙ РЕЗУЛЬТАТ V2 (НЕ записано в БД, не отправлено):")
        print(f"{'='*70}")
        print(f"\n--- summary_short (canonical) ---")
        print(final_short[:1500] + ("..." if len(final_short) > 1500 else ""))
        print(f"\n--- summary_detailed первые 800 chars ---")
        print(final_detailed[:800] + ("..." if len(final_detailed) > 800
                                       else ""))
        print(f"\n--- tasks ({len(final_tasks)}) с owner-trace ---")
        for i, t in enumerate(final_tasks, 1):
            raw_o = t["raw_owner_mention"]
            owner = t["canonical_owner"] or "(no_match)"
            print(f"  {i:2d}) {t['title_canonical'][:80]}")
            print(f"      raw_owner=«{raw_o}» → «{owner}» • "
                  f"priority={t.get('priority','medium')} "
                  f"due={t.get('due_date') or '—'}")
            if t["owner_reasoning"]:
                print(f"      reasoning: {t['owner_reasoning'][:100]}")
            if t["title"] != t["title_canonical"]:
                print(f"      title changed:")
                print(f"        before: «{t['title'][:80]}»")
                print(f"        after:  «{t['title_canonical'][:80]}»")

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
