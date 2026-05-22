"""FR-CR-05-198 — V2 publish: run V2 pipeline, overwrite DB tasks/summary,
publish to Slack channel.

Использование:
    docker exec manager-zoom-ff-1 python -m ops.v2_publish_meeting \\
        --fireflies-id "01KS8ADGW86R630RN1GD9XDYT1"

    docker exec manager-zoom-ff-1 python -m ops.v2_publish_meeting \\
        --zoom-id "Nv+4Cye/TlWsngtC5ZZZiQ=="

Что делает:
  1. Читает MeetingRecording / ZoomRecording из БД (нужен transcript_text).
  2. Прогоняет V2: reasoning extract → matcher → rewrite → apply_task_owner.
  3. Soft-deletes существующих tasks для этого meeting'а (legacy).
  4. Создаёт новые tasks из V2 outputs (с DELEGATE-ed owner).
  5. Перезаписывает row.detailed_summary, row.short_summary V2 версией.
  6. Публикует short_summary + tasks в Slack channel (AUTO_SEND_TO_SLACK_CHANNEL).
  7. Помечает row.short_summary_sent=True.

В отличие от dry-run этот скрипт ПИШЕТ в БД и ОТПРАВЛЯЕТ в Slack.
Использовать только когда dry-run результат удовлетворителен.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from openai import OpenAI

from app.config import get_settings
from app.db import session_scope
from app.intent.llm_backends import OpenAIBackend
from app.logging_setup import get_logger, setup_logging
from app.models import (
    MeetingRecording, Task, TaskSourceKind, ZoomRecording,
)
from app.services.counterparty_aliases import get_orgs_with_aliases
from app.services.entity_apply import apply_task_owner
from app.services.entity_matcher import match_entities
from app.services.entity_rewrite import rewrite_with_canonicals
from app.services.reasoning_extract import extract_summary_and_tasks
from app.services.slack_publish import publish_zoom_recording_to_slack
from app.services.team_member_canonical import (
    canonicalize_participants_via_llm,
)
from app.services.team_members import get_humans_for_matcher

log = get_logger(__name__)


def _extract_meeting_participants(row) -> list[str]:
    """Тот же logic что в dry-run скриптах — фильтр resources, emails OK."""
    from app.agenda.service import _is_resource_attendee
    out: list[str] = []
    if getattr(row, "calendar_attendees", None):
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
                if name and name not in out:
                    out.append(name)
        except Exception:  # noqa: BLE001
            pass
    if not out and getattr(row, "participants", None):
        try:
            for p in row.participants:
                if _is_resource_attendee(p):
                    continue
                s = str(p).strip()
                if s and s not in out:
                    out.append(s)
        except Exception:  # noqa: BLE001
            pass
    return out


def _run_v2_pipeline(*, row, session, llm, model) -> dict:
    """Прогон V2 без записи. Returns dict с финальными V2 outputs."""
    transcript = row.transcript_text or ""
    if not transcript.strip():
        return {"error": "transcript_text empty"}

    print(f"[V2/1] reasoning extract ({len(transcript)} chars)...")
    step1 = extract_summary_and_tasks(
        transcript=transcript,
        meeting_date=(row.meeting_date.isoformat()
                      if row.meeting_date else ""),
        duration_seconds=row.duration_seconds or 0,
        llm_backend=llm, model=model,
    )
    print(f"      → {len(step1['tasks'])} raw tasks, "
          f"detailed={len(step1['summary_detailed'])} chars, "
          f"short={len(step1['summary_short'])} chars")

    raw_owners = [t["raw_owner_mention"] for t in step1["tasks"]]
    known_people = get_humans_for_matcher(session)
    known_orgs = get_orgs_with_aliases(session)
    raw_participants = _extract_meeting_participants(row)
    print(f"[V2/2a] canonical participants ({len(raw_participants)} raw)...")
    meeting_participants = canonicalize_participants_via_llm(
        raw_participants, known_people=known_people,
        llm_backend=llm, model=model,
    )
    print(f"        → {meeting_participants}")

    text_for_match = (
        step1["summary_detailed"] + "\n\n"
        + step1["summary_short"] + "\n\n"
        + "\n".join(
            (t["title"] + " " + (t.get("description") or ""))
            for t in step1["tasks"]
        )
    )
    print(f"[V2/2b] matcher...")
    step2 = match_entities(
        text=text_for_match, raw_owners=raw_owners,
        known_people=known_people, known_orgs=known_orgs,
        meeting_participants=meeting_participants,
        llm_backend=llm, model=model,
    )
    task_owners_list = step2.get("task_owners") or []
    people_repls = step2.get("summary_replacements_people") or []
    orgs_repls = step2.get("summary_replacements_orgs") or []

    print(f"[V2/3] LLM rewrite ({len(step1['tasks'])} tasks + 2 summaries)...")
    final_detailed = rewrite_with_canonicals(
        step1["summary_detailed"],
        people_replacements=people_repls,
        org_replacements=orgs_repls,
        llm_backend=llm, model=model, reasoning_effort="low",
    )
    final_short = rewrite_with_canonicals(
        step1["summary_short"],
        people_replacements=people_repls,
        org_replacements=orgs_repls,
        llm_backend=llm, model=model, reasoning_effort="low",
    )

    # Apply tasks: rewrite text + apply_task_owner для DELEGATE swap
    final_tasks: list[dict] = []
    for idx, t in enumerate(step1["tasks"]):
        raw_o = t["raw_owner_mention"]
        owner_info = (
            task_owners_list[idx] if idx < len(task_owners_list) else {}
        )
        matcher_canonical = owner_info.get("tm_real_name")
        reasoning = owner_info.get("reasoning") or ""
        applied = apply_task_owner(
            {"raw_owner_mention": raw_o, "title": t["title"]},
            tm_real_name=matcher_canonical,
            session=session, matcher_reasoning=reasoning,
        )
        title_canon = rewrite_with_canonicals(
            t["title"], people_replacements=people_repls,
            org_replacements=orgs_repls,
            llm_backend=llm, model=model, reasoning_effort="low",
        )
        desc_canon = rewrite_with_canonicals(
            t.get("description") or "",
            people_replacements=people_repls,
            org_replacements=orgs_repls,
            llm_backend=llm, model=model, reasoning_effort="low",
        )
        final_tasks.append({
            "raw_owner": raw_o,
            "matcher_canonical": matcher_canonical,
            "owner_display_name": applied.get("owner_display_name"),
            "owner_user_id": applied.get("owner_user_id"),
            "matcher_meta": applied.get("matcher_meta") or {},
            "title": title_canon or t["title"],
            "description": desc_canon or (t.get("description") or ""),
            "priority": t.get("priority", "medium"),
            "due_date": t.get("due_date"),
            "reasoning": reasoning,
        })

    return {
        "detailed_summary": final_detailed,
        "short_summary": final_short,
        "tasks": final_tasks,
        "people_replacements": people_repls,
        "orgs_replacements": orgs_repls,
        "meeting_participants": meeting_participants,
    }


def _replace_tasks_in_db(
    *, session, source_kind, source_conversation_id, v2_tasks: list[dict],
) -> tuple[int, int]:
    """Soft-delete legacy tasks, insert V2 tasks. Returns (deleted, inserted)."""
    from datetime import date as _date

    # Soft-delete existing
    existing = (
        session.query(Task)
        .filter(Task.source_kind == source_kind)
        .filter(Task.source_conversation_id == source_conversation_id)
        .filter(Task.deleted_at.is_(None))
        .all()
    )
    now = datetime.now(timezone.utc)
    for t in existing:
        t.deleted_at = now
    session.flush()
    deleted = len(existing)

    # Insert new
    inserted = 0
    for v in v2_tasks:
        title = (v.get("title") or "").strip()[:512]
        if not title:
            continue
        # Parse due_date string → date
        due = None
        if v.get("due_date"):
            try:
                due = _date.fromisoformat(v["due_date"])
            except (ValueError, TypeError):
                pass

        priority = v.get("priority") or "medium"
        meta = v.get("matcher_meta") or {}
        status = "proposed"
        if meta.get("status") == "delegated":
            status = "todo"  # delegate auto-confirm

        new_task = Task(
            title=title,
            description=v.get("description") or None,
            owner_display_name=v.get("owner_display_name"),
            owner_user_id=v.get("owner_user_id"),
            priority=priority,
            status=status,
            due_date=due,
            source_kind=source_kind,
            source_conversation_id=source_conversation_id,
            extra={"matcher_meta": meta, "v2": True},
        )
        session.add(new_task)
        inserted += 1
    session.flush()
    return deleted, inserted


def _format_v2_task_for_todo(v: dict, idx: int) -> str:
    """Mimic `_build_todo_section` format для V2 in-memory task'и."""
    from datetime import date as _date, datetime as _dt

    title = (v.get("title") or "").strip()
    desc = (v.get("description") or "").strip()
    owner = (v.get("owner_display_name") or "").strip()
    body = desc or title
    if len(body) > 350:
        cut = body.rfind(" ", 0, 350)
        body = (body[: cut if cut > 200 else 350]).rstrip(",;:- ") + "…"
    due_str = v.get("due_date") or ""
    try:
        due_date = _date.fromisoformat(due_str) if due_str else _date.today()
    except (ValueError, TypeError):
        due_date = _date.today()
    deadline = f"{due_date.strftime('%d.%m.%Y')} 18:00"
    suffix_parts = []
    if owner:
        suffix_parts.append(owner)
    suffix_parts.append(deadline)
    suffix = " • ".join(suffix_parts)
    return f"{idx}) {body} — {suffix}"


def _build_todo_text_from_v2(v2_tasks: list[dict], *, filter_important: bool) -> str:
    """Inline-build TODO block из in-memory V2 tasks. БД не читаем."""
    from app.services.task_direction import DIRECTIONS_IMPORTANT
    items: list[str] = []
    idx = 0
    for v in v2_tasks:
        direction = v.get("direction") or "other"
        if filter_important and direction not in DIRECTIONS_IMPORTANT:
            continue
        idx += 1
        items.append(_format_v2_task_for_todo(v, idx))
    if not items:
        return ""
    return "To-Do:\n\n" + "\n\n".join(items)


def main() -> int:
    setup_logging()
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--fireflies-id")
    g.add_argument("--zoom-id")
    ap.add_argument(
        "--read-only", action="store_true",
        help="One-off: НЕ писать V2 в БД (задачи уже есть). По умолчанию "
        "OFF — production behavior — V2 results пишутся в БД (заменяя "
        "legacy для этого meeting'а).",
    )
    ap.add_argument("--no-publish", action="store_true",
                    help="Только compute V2 (без Slack-публикации)")
    args = ap.parse_args()

    s = get_settings()
    oc = OpenAI(api_key=s.openai_api_key)
    llm = OpenAIBackend(client=oc, model=s.fireflies_tasks_model)
    model = s.fireflies_tasks_model

    with session_scope() as session:
        # Resolve row + source_kind
        if args.fireflies_id:
            row = session.query(MeetingRecording).filter(
                MeetingRecording.fireflies_id == args.fireflies_id
            ).first()
            source_kind = TaskSourceKind.fireflies
            source_id = args.fireflies_id
            label = f"fireflies_id={args.fireflies_id}"
        else:
            row = session.query(ZoomRecording).filter(
                ZoomRecording.zoom_id == args.zoom_id
            ).first()
            source_kind = TaskSourceKind.zoom
            source_id = args.zoom_id
            label = f"zoom_id={args.zoom_id}"

        if row is None:
            print(f"ERROR: {label} not found", file=sys.stderr)
            return 1
        if not (row.transcript_text or "").strip():
            print(f"ERROR: transcript_text empty for {label}", file=sys.stderr)
            return 2

        print(f"\n{'='*70}")
        print(f"V2 PUBLISH для {label}")
        write_db = not args.read_only
        print(f"title={row.title!r}, write_db={write_db}")
        print(f"{'='*70}\n")

        # Run V2 — in-memory
        result = _run_v2_pipeline(row=row, session=session, llm=llm, model=model)
        if "error" in result:
            print(f"ERROR V2: {result['error']}", file=sys.stderr)
            return 3

        print(f"\nV2 results (in-memory):")
        print(f"  detailed_summary: {len(result['detailed_summary'])} chars")
        print(f"  short_summary: {len(result['short_summary'])} chars")
        print(f"  tasks: {len(result['tasks'])}")
        owners_count: dict[str, int] = {}
        for t in result["tasks"]:
            owner = t["owner_display_name"] or "(no_match)"
            owners_count[owner] = owners_count.get(owner, 0) + 1
        for owner, n in sorted(owners_count.items(), key=lambda x: -x[1]):
            print(f"    {n:2d}× {owner}")

        # Classify directions ДЛЯ V2 tasks (in-memory только)
        from app.services.task_direction import (
            DIRECTIONS_IMPORTANT, classify_directions,
        )
        print(f"\nClassifying directions для V2 tasks (in-memory)...")
        # Тасуем фейковые id для mapping
        for i, v in enumerate(result["tasks"]):
            v["_id"] = i
        tasks_to_classify = [
            {"id": v["_id"], "title": v.get("title") or "",
             "description": v.get("description") or ""}
            for v in result["tasks"]
        ]
        mapping = classify_directions(
            tasks=tasks_to_classify,
            meeting_context=result["detailed_summary"][:3000] or None,
            llm_backend=llm, model=model,
        )
        for v in result["tasks"]:
            v["direction"] = mapping.get(v["_id"], "other")
        important_count = sum(
            1 for d in mapping.values() if d in DIRECTIONS_IMPORTANT
        )
        print(f"  classified {len(mapping)} tasks, "
              f"{important_count} в DIRECTIONS_IMPORTANT (попадут в Slack)")

        # OPTIONAL: write to DB (если --write-db)
        if write_db:
            print(f"\n[--write-db] Replacing DB tasks (legacy → V2)...")
            deleted, inserted = _replace_tasks_in_db(
                session=session, source_kind=source_kind,
                source_conversation_id=source_id, v2_tasks=result["tasks"],
            )
            print(f"  soft-deleted {deleted}, inserted {inserted}")
            row.detailed_summary = result["detailed_summary"]
            row.short_summary = result["short_summary"]
            # Save direction'ы в task.extra
            new_db_tasks = (
                session.query(Task)
                .filter(Task.source_kind == source_kind)
                .filter(Task.source_conversation_id == source_id)
                .filter(Task.deleted_at.is_(None))
                .order_by(Task.id.asc()).all()
            )
            for db_t, v in zip(new_db_tasks, result["tasks"]):
                extra = dict(db_t.extra or {})
                extra["direction"] = v.get("direction", "other")
                db_t.extra = extra
            session.flush()

        # Slack publish — БД не читаем (override_thread_todo_text)
        if not args.no_publish:
            from app.services.slack_publish import (
                _get_channel, _get_token_key,
            )
            channel = _get_channel() or "D0ASY5QF6UX"
            token_key = _get_token_key()
            token = getattr(s, token_key, "") or ""
            if not token:
                print(f"ERROR: settings.{token_key} empty",
                      file=sys.stderr)
                return 4

            # Build V2 todo block из in-memory tasks (filtered by direction)
            v2_todo_text = _build_todo_text_from_v2(
                result["tasks"], filter_important=True,
            )

            print(f"\nPublishing to Slack channel {channel} via {token_key}...")
            print(f"  parent: V2 short_summary ({len(result['short_summary'])} chars)")
            print(f"  thread: {important_count} important V2 tasks")
            pub_result = publish_zoom_recording_to_slack(
                session, row,
                channel=channel, token=token,
                no_tasks=False,
                # FR-CR-05-199 — БД не трогаем, V2 contents in-memory:
                override_short_summary=result["short_summary"],
                override_thread_todo_text=v2_todo_text,
                skip_db_write=not write_db,
            )
            print(f"  publish result: {pub_result}")

        # Commit только если был write_db
        if write_db:
            session.commit()
        else:
            session.rollback()  # явный rollback — гарантируем БД нетронута

        print(f"\n{'='*70}")
        print(f"V2 PUBLISH done для {label}")
        if not write_db:
            print("DB НЕ изменена (--write-db не передан).")
        print(f"{'='*70}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
