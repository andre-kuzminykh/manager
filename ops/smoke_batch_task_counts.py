"""FR-CR-05-185 follow-up — batch task-count smoke across a date
range of ZoomRecording rows. For each viable row (transcript +
detailed_summary present), calls the TASK_EXTRACTION_SYSTEM LLM
directly and counts how many tasks (and how many with explicit
due_date) it emits.

NO database INSERT/UPDATE. NO Slack post. Read-only verification.

Operator-pinned 2026-05-21: «дальше прогоним но в слак не пустим,
в бд тоже ничего не пускаем, просто проверим количество задач».

Usage:
    docker compose exec -T bot python -m ops.smoke_batch_task_counts \\
        --start 2026-05-19 --end 2026-05-22 --min-minutes 10
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone

from app.config import get_settings
from app.db import session_scope
from app.fireflies.prompts import TASK_EXTRACTION_SYSTEM
from app.intent.llm_backends import OpenAIBackend
from app.models import ZoomRecording


def _build_user_prompt(row, today_iso: str) -> str:
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
        f"today_date: {today_iso}\n"
        f"\nmeeting_participants:\n{participants_lines}\n"
    )
    return (
        "Return JSON: `{\"tasks\": [{\"title\": ..., "
        "\"description\": ..., \"owner\": ..., "
        "\"priority\": ..., \"due_date\": "
        "\"YYYY-MM-DD or omit\", \"due_time\": "
        "\"HH:MM or omit\"}, ...]}`. Empty list ok.\n\n"
        + meta
        + "\nТранскрипт встречи:\n"
        + (row.transcript_text or "")
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--min-minutes", type=int, default=10)
    args = ap.parse_args()

    s = get_settings()
    if not s.openai_api_key:
        print("ERROR: OPENAI_API_KEY not set.", file=sys.stderr)
        return 2

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    min_secs = args.min_minutes * 60
    today_iso = date.today().isoformat()

    with session_scope() as session:
        rows = session.query(ZoomRecording).filter(
            ZoomRecording.meeting_date >= start,
            ZoomRecording.meeting_date < end,
        ).order_by(ZoomRecording.meeting_date.asc()).all()
        rows = [r for r in rows if (r.duration_seconds or 0) >= min_secs]
        viable = [
            r for r in rows
            if (r.transcript_text or "").strip()
            and (r.detailed_summary or "").strip()
        ]
        skipped = len(rows) - len(viable)
        print(
            f"\n# Batch task-count smoke {args.start} .. {args.end} "
            f"(≥ {args.min_minutes} min) — {len(viable)} viable rows "
            f"({skipped} skipped: no transcript/detailed_summary). "
            f"today_date={today_iso}\n"
        )

        from openai import OpenAI
        oc = OpenAI(api_key=s.openai_api_key)
        llm = OpenAIBackend(client=oc, model=s.fireflies_tasks_model)

        print(
            f"{'#':<3} {'date':<13} {'tasks':<6} {'w/date':<7} "
            f"{'chars':<7} title"
        )
        print("-" * 110)
        totals = {"tasks": 0, "with_due_date": 0, "with_due_time": 0,
                  "calls": 0, "failed": 0}
        for i, r in enumerate(viable, start=1):
            up = _build_user_prompt(r, today_iso)
            try:
                raw = llm.complete_text(
                    system_prompt=TASK_EXTRACTION_SYSTEM,
                    user_prompt=up,
                    model=s.fireflies_tasks_model,
                    reasoning_effort=(
                        s.fireflies_tasks_reasoning_effort or None
                    ),
                    response_format={"type": "json_object"},
                ) or ""
                parsed = json.loads(raw) if raw else {}
                tasks = parsed.get("tasks") or []
                n = len(tasks)
                wd = sum(1 for t in tasks if isinstance(t, dict) and t.get("due_date"))
                wt = sum(1 for t in tasks if isinstance(t, dict) and t.get("due_time"))
                totals["tasks"] += n
                totals["with_due_date"] += wd
                totals["with_due_time"] += wt
                totals["calls"] += 1
            except Exception as e:  # noqa: BLE001
                n = wd = wt = 0
                totals["failed"] += 1
                err = str(e)[:40]
                print(
                    f"{i:<3} {r.meeting_date.strftime('%m-%d %H:%M'):<13} "
                    f"ERR {err:<60}"
                )
                continue
            print(
                f"{i:<3} {r.meeting_date.strftime('%m-%d %H:%M'):<13} "
                f"{n:<6} {wd:<7} "
                f"{len(r.transcript_text or '')//1000:>3}k    "
                f"{(r.title or '')[:55]}"
            )
        print("-" * 110)
        print(
            f"\nTotals: calls={totals['calls']} "
            f"failed={totals['failed']} "
            f"tasks={totals['tasks']} "
            f"with_due_date={totals['with_due_date']} "
            f"with_due_time={totals['with_due_time']}\n"
            f"NO database INSERTs / UPDATEs. NO Slack posts. "
            f"Read-only verification."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
