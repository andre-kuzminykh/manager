"""FR-CR-05-193a — Step 1 of Entity Resolution V2.

Single reasoning LLM call: transcript → JSON {summary_detailed,
summary_short, tasks: [...]}.
NO entity resolution. Raw mentions only.
"""
from __future__ import annotations

import json
from typing import Any

from app.logging_setup import get_logger

log = get_logger(__name__)


_SYSTEM_PROMPT = """Ты опытный аналитик встречи. Тебе дан транскрипт зум/Fireflies записи.

Твоя задача — один reasoning pass:
  1. Длинное саммари (summary_detailed) — детальное описание встречи (3-10 параграфов).
  2. Короткое саммари (summary_short) — 2-4 предложения, суть.
  3. Список задач (tasks) — actionable items с raw_owner_mention как
     они упомянуты в транскрипте.

ВАЖНО: НЕ пытайся резолвить кто такой "Дима" / "Schaeffler" — пиши raw mention
как в транскрипте. Резолв сущностей идёт отдельным шагом потом.

Возвращай СТРОГО JSON в формате:
{
  "summary_detailed": "...",
  "summary_short": "...",
  "tasks": [
    {
      "raw_owner_mention": "Дима",
      "title": "Подготовить материалы для Schaeffler",
      "description": "До среды собрать pitch deck",
      "due_date": "2026-05-25",
      "priority": "high"
    }
  ]
}

Поля задачи:
  - raw_owner_mention (required): имя как в транскрипте
  - title (required): краткое название задачи
  - description (optional): расширенное описание
  - due_date (optional, ISO YYYY-MM-DD): если упомянут срок
  - priority (optional, default "medium"): low | medium | high | urgent
"""


def extract_summary_and_tasks(
    *,
    transcript: str | None,
    meeting_date: str,
    duration_seconds: int,
    llm_backend: Any,
    model: str,
) -> dict:
    """FR-CR-05-193a — single reasoning LLM call.

    Returns dict {summary_detailed, summary_short, tasks: list}.
    Безопасный fallback на empty inputs / malformed LLM output.
    """
    empty = {"summary_detailed": "", "summary_short": "", "tasks": []}

    if not transcript or not transcript.strip():
        return empty

    log.info("reasoning_extract_started",
             transcript_chars=len(transcript),
             meeting_date=meeting_date,
             duration_seconds=duration_seconds)

    user_prompt = (
        f"Встреча {meeting_date}, длительность {duration_seconds}s.\n\n"
        f"Транскрипт:\n{transcript}\n\n"
        f"Верни JSON по контракту."
    )

    try:
        raw = llm_backend.complete_text(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model=model,
            reasoning_effort="high",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("reasoning_extract_llm_error", error=str(e))
        return empty

    if not raw or not str(raw).strip():
        log.warning("reasoning_extract_empty_response")
        return empty

    # Strip ```json``` fences if present
    text = str(raw).strip()
    if text.startswith("```"):
        # remove leading/trailing code fences
        text = text.lstrip("`").lstrip("json").strip()
        if text.endswith("```"):
            text = text[: -3].strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        log.warning("reasoning_extract_json_error", error=str(e),
                    raw_head=text[:200])
        return empty

    # Validate + clean
    summary_d = (parsed.get("summary_detailed") or "").strip()
    summary_s = (parsed.get("summary_short") or "").strip()
    raw_tasks = parsed.get("tasks") or []
    if not isinstance(raw_tasks, list):
        raw_tasks = []

    clean_tasks: list[dict] = []
    for t in raw_tasks:
        if not isinstance(t, dict):
            continue
        owner = (t.get("raw_owner_mention") or "").strip()
        title = (t.get("title") or "").strip()
        if not owner or not title:
            continue  # drop incomplete
        clean = {
            "raw_owner_mention": owner,
            "title": title,
            "description": (t.get("description") or "").strip() or None,
            "due_date": (t.get("due_date") or "").strip() or None,
            "priority": (t.get("priority") or "medium").strip().lower(),
        }
        if clean["priority"] not in ("low", "medium", "high", "urgent"):
            clean["priority"] = "medium"
        clean_tasks.append(clean)

    log.info("reasoning_extract_done",
             tasks=len(clean_tasks),
             summary_detailed_chars=len(summary_d),
             summary_short_chars=len(summary_s))

    return {
        "summary_detailed": summary_d,
        "summary_short": summary_s,
        "tasks": clean_tasks,
    }
