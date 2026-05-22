"""FR-CR-05-193 dry-run — прогнать V2 (Step 1 reasoning + Step 2 matcher +
Step 3 apply) для одной Fireflies-записи и вывести результат БЕЗ записи в
БД, Slack или TG. Чистая diagnostic печать.

Usage:
    docker exec manager-zoom-ff-1 python -m ops.v2_dry_run_fireflies \\
        --fireflies-id "01KS5PBJ7EQS4311TCXK5V7QMQ"
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
from app.models import MeetingRecording
from app.services.entity_rewrite import rewrite_with_canonicals
from app.services.entity_matcher import match_entities
from app.services.reasoning_extract import extract_summary_and_tasks
from app.services.team_members import get_humans_for_matcher
from app.services.counterparty_aliases import get_orgs_with_aliases

log = get_logger(__name__)


def main() -> int:
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--fireflies-id", required=True)
    args = ap.parse_args()

    s = get_settings()
    oc = OpenAI(api_key=s.openai_api_key)
    llm = OpenAIBackend(client=oc, model=s.fireflies_tasks_model)
    model_reasoning = s.fireflies_tasks_model  # gpt-5.5
    model_matcher = s.fireflies_tasks_model

    with session_scope() as session:
        row = session.query(MeetingRecording).filter(
            MeetingRecording.fireflies_id == args.fireflies_id
        ).first()
        if row is None:
            print(f"ERROR: fireflies_id={args.fireflies_id} not found", file=sys.stderr)
            return 1
        if not (row.transcript_text or "").strip():
            print("ERROR: transcript_text empty — нужно сначала "
                  "прогнать pipeline хотя бы до transcribe step",
                  file=sys.stderr)
            return 2

        print(f"\n{'='*70}")
        print(f"V2 DRY-RUN для fireflies_id={args.fireflies_id}")
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
        # FR-CR-05-192ac — фильтруем resources (переговорки), оставляем
        # emails (это идентификаторы людей, может резолвить LLM canon).
        from app.agenda.service import _is_resource_attendee
        meeting_participants: list[str] = []
        if row.calendar_attendees:
            try:
                for a in row.calendar_attendees:
                    if _is_resource_attendee(a):
                        continue
                    if isinstance(a, dict):
                        name = (a.get("resolved_name")
                                or a.get("display_name")
                                or a.get("email")
                                or "").strip()
                    else:
                        name = str(a).strip()
                    if name and name not in meeting_participants:
                        meeting_participants.append(name)
            except Exception:  # noqa: BLE001
                pass
        if not meeting_participants and row.participants:
            try:
                for p in row.participants:
                    if _is_resource_attendee(p):
                        continue
                    s = str(p).strip()
                    if s and s not in meeting_participants:
                        meeting_participants.append(s)
            except Exception:  # noqa: BLE001
                pass
        print(f"  meeting_participants (raw from calendar): "
              f"{len(meeting_participants)} → {meeting_participants}")

        # FR-CR-05-193b-7 — нормализовать к canonical TM real_name ЧЕРЕЗ LLM.
        # Иначе scrub отвергает английские tm_real_name из matcher'а когда
        # whitelist на русском (или наоборот).
        from app.services.team_member_canonical import (
            canonicalize_participants_via_llm,
        )
        meeting_participants = canonicalize_participants_via_llm(
            meeting_participants,
            known_people=known_people,
            llm_backend=llm, model=model_matcher,
        )
        print(f"  meeting_participants (canonical TM real_name): "
              f"{len(meeting_participants)} → {meeting_participants}")

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

        # === Step 3: LLM-rewrite (FR-CR-05-193c-3) ===
        # Заменили regex apply_text_replacements на LLM rewrite — для
        # правильных русских падежей («Лене» → «Елене Радионовой» вместо
        # «Радионова Елена»).
        print(f"\n[Step 3/3] LLM rewrite WITH MAPPINGS (правильные падежи)...")
        people_repls = step2.get("summary_replacements_people") or []
        orgs_repls = step2.get("summary_replacements_orgs") or []

        # Per-replacement preview: count occurrences в каждом фрагменте
        # (показывает что именно LLM получит на вход)
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

        print(f"\n  Rewriting summary_detailed ({len(step1['summary_detailed'])} chars)...")
        final_detailed = rewrite_with_canonicals(
            step1["summary_detailed"],
            people_replacements=people_repls,
            org_replacements=orgs_repls,
            llm_backend=llm, model=model_reasoning,
            reasoning_effort="low",
        )
        print(f"  Rewriting summary_short ({len(step1['summary_short'])} chars)...")
        final_short = rewrite_with_canonicals(
            step1["summary_short"],
            people_replacements=people_repls,
            org_replacements=orgs_repls,
            llm_backend=llm, model=model_reasoning,
            reasoning_effort="low",
        )

        # tasks final — owner через DB lookup (apply_task_owner-like),
        # text (title/description) через LLM rewrite.
        # FR-CR-05-193c-4: matcher.task_owners — это LIST в том же порядке
        # что raw_owners был передан. Мапим по INDEX, не по raw_owner
        # (несколько задач могут иметь одинаковый raw_owner типа «мы»).
        # FR-CR-05-193c-5: применяем apply_task_owner для DELEGATE swap'a
        # (видно в выводе если у TeamMember notes есть DELEGATE_TASKS_TO).
        from app.services.entity_apply import apply_task_owner
        task_owners_list = step2.get("task_owners") or []
        print(f"  Rewriting {len(step1['tasks'])} task titles+descriptions...")
        final_tasks = []
        for idx, t in enumerate(step1["tasks"]):
            raw_o = t["raw_owner_mention"]
            owner_info = (
                task_owners_list[idx] if idx < len(task_owners_list) else {}
            )
            canonical = owner_info.get("tm_real_name")
            reasoning = owner_info.get("reasoning") or ""
            # Apply DELEGATE_TASKS_TO + DO_NOT_CALL DSL — swap при необходимости
            applied = apply_task_owner(
                {"raw_owner_mention": raw_o, "title": t["title"]},
                tm_real_name=canonical,
                session=session,
                matcher_reasoning=reasoning,
            )
            final_owner = applied.get("owner_display_name")
            delegate_meta = applied.get("matcher_meta") or {}
            # LLM rewrite для title+description (склонения)
            title_canon = rewrite_with_canonicals(
                t["title"],
                people_replacements=people_repls,
                org_replacements=orgs_repls,
                llm_backend=llm, model=model_reasoning,
                reasoning_effort="low",
            )
            desc_canon = rewrite_with_canonicals(
                t.get("description") or "",
                people_replacements=people_repls,
                org_replacements=orgs_repls,
                llm_backend=llm, model=model_reasoning,
                reasoning_effort="low",
            )
            final_tasks.append({
                **t,
                "matcher_canonical": canonical,  # raw matcher output
                "final_owner": final_owner,  # после DELEGATE swap
                "delegate_status": delegate_meta.get("status"),
                "delegate_original": delegate_meta.get("original_owner"),
                "canonical_owner": final_owner or canonical,
                "owner_reasoning": reasoning,
                "title_canonical": title_canon,
                "description_canonical": desc_canon,
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
            matcher_pick = t.get("matcher_canonical")
            final_o = t.get("final_owner") or t["canonical_owner"]
            delegate_status = t.get("delegate_status")
            delegate_original = t.get("delegate_original")
            print(f"  {i:2d}) {t['title_canonical'][:80]}")
            if delegate_status == "delegated" and delegate_original:
                # Видно DELEGATE swap: raw → matcher → delegate target
                print(f"      raw_owner=«{raw_o}» → matcher: «{matcher_pick}» "
                      f"→ delegate: «{final_o}» • "
                      f"priority={t.get('priority','medium')} "
                      f"due={t.get('due_date') or '—'}")
            else:
                owner = final_o or "(no_match)"
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
            Task.source_kind == TaskSourceKind.fireflies,
            Task.source_conversation_id == row.fireflies_id,
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
