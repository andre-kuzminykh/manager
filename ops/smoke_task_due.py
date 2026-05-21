"""FR-CR-05-185 verification — call TASK_EXTRACTION_SYSTEM on a real
ZoomRecording row and print the LLM-emitted due_date / due_time for
each task. NO database INSERTs. Operator-pinned 2026-05-21:
«проверь deadline-extraction, но таски в бд не пойдут».

Usage:
    docker compose exec -T bot python -m ops.smoke_task_due \\
        --zoom-id "..."
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from app.config import get_settings
from app.db import session_scope
from app.fireflies.prompts import TASK_EXTRACTION_SYSTEM
from app.intent.llm_backends import OpenAIBackend
from app.models import ZoomRecording


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zoom-id", required=True)
    args = ap.parse_args()

    s = get_settings()
    if not s.openai_api_key:
        print("ERROR: OPENAI_API_KEY not set.", file=sys.stderr)
        return 2

    with session_scope() as session:
        row = session.query(ZoomRecording).filter(
            ZoomRecording.zoom_id == args.zoom_id
        ).first()
        if row is None:
            print(f"ERROR: no row for zoom_id={args.zoom_id!r}",
                  file=sys.stderr)
            return 3
        if not (row.detailed_summary or "").strip():
            print(f"ERROR: row.detailed_summary is empty — run "
                  "reprocess first", file=sys.stderr)
            return 4

        # Build participants line from calendar_attendees (matches
        # FR-CR-05-181 contract).
        cal_names: list[str] = []
        for a in (row.calendar_attendees or []):
            if not isinstance(a, dict):
                continue
            nm = (a.get("resolved_name") or a.get("display_name")
                  or a.get("email") or "").strip()
            if nm:
                cal_names.append(nm)
        participants_lines = (
            "\n".join(f"  - {p}" for p in cal_names if p)
            or "  (нет данных)"
        )

        meta = (
            f"meeting_title: {row.title or ''}\n"
            f"meeting_date: {row.meeting_date.isoformat() if row.meeting_date else ''}\n"
            f"today_date: {date.today().isoformat()}\n"
            f"\nmeeting_participants (REAL NAMES of who was on this call):\n"
            f"{participants_lines}\n"
        )
        user_prompt = (
            "Return JSON: `{\"tasks\": [{\"title\": ..., "
            "\"description\": ..., \"owner\": ..., "
            "\"priority\": ..., \"due_date\": "
            "\"YYYY-MM-DD or omit\", \"due_time\": "
            "\"HH:MM or omit\"}, ...]}`. Empty list ok.\n\n"
            + meta
            + "\nТранскрипт встречи:\n"
            + (row.transcript_text or "")
        )

    from openai import OpenAI
    oc = OpenAI(api_key=s.openai_api_key)
    llm = OpenAIBackend(client=oc, model=s.fireflies_tasks_model)

    print(f"\n[1/2] Calling LLM ({s.fireflies_tasks_model}, "
          f"reasoning_effort={s.fireflies_tasks_reasoning_effort}, "
          f"transcript_chars={len(row.transcript_text or '')}, "
          f"today_date={date.today().isoformat()})…")
    try:
        raw = llm.complete_text(
            system_prompt=TASK_EXTRACTION_SYSTEM,
            user_prompt=user_prompt,
            model=s.fireflies_tasks_model,
            reasoning_effort=(
                s.fireflies_tasks_reasoning_effort or None
            ),
            response_format={"type": "json_object"},
        ) or ""
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: LLM call failed: {e}", file=sys.stderr)
        return 5

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        print(f"ERROR: response is not valid JSON:\n{raw[:500]}",
              file=sys.stderr)
        return 6
    tasks = parsed.get("tasks") or []
    print(f"      → {len(tasks)} tasks returned")

    print("\n[2/2] Per-task due_date / due_time:")
    print("-" * 95)
    print(f"{'#':<3} {'due_date':<12} {'due_time':<8} {'owner':<30} title")
    print("-" * 95)
    with_due_date = 0
    with_due_time = 0
    for i, t in enumerate(tasks, start=1):
        if not isinstance(t, dict):
            continue
        dd = t.get("due_date") or "—"
        dt = t.get("due_time") or "—"
        title = (t.get("title") or "")[:60]
        owner = (t.get("owner") or "")[:28]
        if t.get("due_date"):
            with_due_date += 1
        if t.get("due_time"):
            with_due_time += 1
        print(f"{i:<3} {dd:<12} {dt:<8} {owner:<30} {title}")
    print("-" * 95)
    print(
        f"\nSummary: {with_due_date}/{len(tasks)} tasks with explicit "
        f"due_date, {with_due_time}/{len(tasks)} with due_time. "
        f"NO database INSERTs (read-only)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
