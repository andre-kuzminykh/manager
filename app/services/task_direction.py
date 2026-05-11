"""FR-CR-05-163 — classify extracted meeting tasks by «направление»
(strategic direction). Important directions get badged in the To-Do
block to call CEO attention. Other tasks render plainly.

Operator-pinned categories (важные задачи):
  - **beta**          — выпуск бета-версий / launch / релизы
  - **budget**        — финансы, P&L, контрактные суммы
  - **design**        — продукт-дизайн / UX
  - **investors**     — fundraising, investor relations, IR
  - **deliverables**  — ключевые контрактные / клиентские deliverables

Anything else → **other** (рутина, рендер без badge'a).
"""
from __future__ import annotations

import json
from typing import Any

from app.logging_setup import get_logger

log = get_logger(__name__)


DIRECTIONS_IMPORTANT: tuple[str, ...] = (
    "beta",
    "budget",
    "design",
    "investors",
    "deliverables",
)

# Set used for fast `in` check; включает 'other' для validate
_VALID_DIRECTIONS: frozenset[str] = frozenset(
    list(DIRECTIONS_IMPORTANT) + ["other"]
)


DIRECTION_BADGES = {
    "beta": "📌 [БЕТА]",
    "budget": "💰 [БЮДЖЕТ]",
    "design": "🎨 [ДИЗАЙН]",
    "investors": "💼 [ИНВЕСТОРЫ]",
    "deliverables": "🎯 [КЛЮЧЕВЫЕ DELIVERABLES]",
}


SYSTEM_PROMPT = """Ты классифицируешь задачи по стратегическим направлениям.

Возможные направления (строго одно из):
- "beta": выпуск бета-версий продукта, релизы, launch, deployment, milestones
- "budget": финансы, бюджеты, P&L, контрактные суммы, фандрайзинг-цифры (НЕ сам процесс fundraising'a, а денежные числа/контракты)
- "design": продукт-дизайн, UX, UI, brand, визуальные ассеты
- "investors": fundraising activities, investor relations (IR), pitches, term sheets, follow-up'ы с инвесторами и фондами
- "deliverables": ключевые клиентские/контрактные deliverables, обязательства перед заказчиками/партнёрами по продукту или сервису
- "other": рутина, операционные задачи, мелочи, которые не подпадают под выше категории

Правила:
- Используй контекст всей встречи если он есть, не только заголовок задачи
- При сомнении между "other" и важной категорией — выбирай ВАЖНУЮ только если задача явно про неё
- "Подготовить отчёт" сам по себе → other, "Подготовить отчёт для инвесторов" → investors
- "Договориться о встрече с инвестором" → investors
- "Договориться о встрече с клиентом" → other (если не deliverables)

Возвращай СТРОГО JSON:
{
  "items": [
    {"task_id": <id>, "direction": "investors", "reasoning": "коротко по-русски"}
  ]
}
"""


def classify_directions(
    *,
    tasks: list[dict[str, Any]],
    meeting_context: str | None,
    llm_backend: Any,
    model: str,
) -> dict[int, str]:
    """Single LLM call → mapping {task_id: direction}.

    Args:
      tasks: list of {"id": int, "title": str, "description": str}
      meeting_context: optional shorter (~1-3 KB) excerpt from the
        detailed_summary; helps disambiguate context-dependent tasks
      llm_backend: app.intent.llm_backends.OpenAIBackend
      model: model name (e.g. settings.fireflies_tasks_model)

    Returns:
      {task_id: direction_string}. Direction ∈ DIRECTIONS_IMPORTANT
      or "other". Tasks not classified by LLM → "other" as fallback.
      Returns empty dict on LLM/parse failure (caller logs).
    """
    if not tasks:
        return {}

    table_lines: list[str] = []
    for t in tasks:
        tid = t.get("id")
        title = (t.get("title") or "").strip()[:200]
        desc = (t.get("description") or "").strip()[:300]
        line = f"id={tid} | title={title}"
        if desc and desc != title:
            line += f" | desc={desc}"
        table_lines.append(line)
    tasks_block = "\n".join(table_lines)

    ctx_block = ""
    if meeting_context and meeting_context.strip():
        ctx_block = (
            "\n\nКОНТЕКСТ ВСТРЕЧИ (для disambiguation):\n"
            + meeting_context.strip()[:3000]
        )

    user_prompt = (
        f"ЗАДАЧИ ({len(tasks)}):\n{tasks_block}{ctx_block}\n\n"
        "Верни JSON с полем items."
    )

    log.info(
        "task_direction_call_started",
        tasks_count=len(tasks), model=model,
        has_context=bool(meeting_context),
    )

    try:
        raw = llm_backend.complete_text(  # type: ignore[attr-defined]
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model=model,
            temperature=0.0,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("task_direction_llm_error", error=str(e))
        return {}

    if not raw or not raw.strip():
        log.warning("task_direction_empty_response")
        return {}

    # Parse JSON. Be lenient — sometimes LLM wraps in ```json ...```
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        parsed = json.loads(text)
    except Exception as e:  # noqa: BLE001
        log.warning("task_direction_json_parse_error", error=str(e), raw=raw[:200])
        return {}

    items = parsed.get("items") if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        log.warning("task_direction_no_items_array")
        return {}

    out: dict[int, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        tid_raw = item.get("task_id")
        direction = (item.get("direction") or "other").strip().lower()
        if direction not in _VALID_DIRECTIONS:
            direction = "other"
        try:
            tid = int(tid_raw)
        except (TypeError, ValueError):
            continue
        out[tid] = direction

    log.info(
        "task_direction_done",
        classified=len(out),
        total=len(tasks),
        important=sum(1 for d in out.values() if d in DIRECTIONS_IMPORTANT),
    )
    return out


__all__ = [
    "DIRECTIONS_IMPORTANT",
    "DIRECTION_BADGES",
    "classify_directions",
]
