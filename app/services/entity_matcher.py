"""FR-CR-05-193b — Step 2: matcher LLM call.

Принимает summary text + tasks raw_owners + known_people (с notes/role)
+ known_orgs (с aliases). Возвращает mappings.
"""
from __future__ import annotations

import json
from typing import Any

from app.logging_setup import get_logger

log = get_logger(__name__)


_SYSTEM_PROMPT = """Ты резолвер сущностей. Тебе дан текст встречи + список
известных людей (TeamMember) + список известных компаний/контрагентов
(Counterparty с aliases) + список участников ЭТОЙ встречи (meeting_participants).

Задача:
  1. Для каждого raw_owner из tasks определи canonical real_name из
     known_people. Если raw не соответствует никому — tm_real_name=null.
  2. Для упоминаний в тексте summary (как людей, так и организаций):
     - имена сотрудников → canonical real_name из known_people
     - имена компаний → canonical name из known_orgs (включая aliases)
  3. Notes из known_people используй для disambiguation:
     - Дима Дроздов: notes="ВСЕ ЧТО СВЯЗАНО С ФОНДАМИ"
     - Дмитрий Седов: notes="ТОЛЬКО РАБОТА С КОНТРАКТАМИ ОТ ФОНДОВ"
     → если контекст про outreach в фонд = Дроздов, контракты = Седов
  4. SPEAKER FALLBACK для дейктических местоимений в raw_owner:
     - Если raw_owner = "я" / "мне" / "мной" / "сама" / "сам" — определи
       кто это вероятно по контексту task'a (например, в Fundraising daily
       команды Артем/Алина/Дима/Ирина — кто отвечает за описанную
       activity).
     - Используй meeting_participants как whitelist кандидатов.
     - Если уверенность низкая — поставь null.
  5. STRICT RULE — task_owners ТОЛЬКО из meeting_participants:
     - tm_real_name ОБЯЗАН быть из списка meeting_participants. Никогда
       НЕ назначай tasks на людей которые не были на этой встрече.
     - Если raw_owner упоминает не-participant (например «Попроси Федю
       сделать X» — а Федя НЕ участник), резолви на participant который
       скорее всего реально будет выполнять (часто это автор задачи /
       координатор / speaker), или null если непонятно.
     - Reasoning должен указать почему именно этот participant выбран.

ВАЖНО:
  - task_owners — ТОЛЬКО из meeting_participants. Если не подходит ни один — null.
  - summary_replacements_people может включать ЛЮБОГО из known_people
    (упоминания не обязаны быть participants — например, в саммари
    может быть упомянут «Федя» как третье лицо, и он резолвится в
    Fedor Pavlovich для canonical написания).
  - НЕ инвентируй новые сущности.
  - summary_replacements — только реально matched (заменяемые), без unmatched.

Возвращай СТРОГО JSON:
{
  "task_owners": [
    {"raw_owner": "Дима", "tm_real_name": "Дима Дроздов", "reasoning": "контекст outreach в фонд"}
  ],
  "summary_replacements_people": [
    {"raw": "Дима", "canonical": "Дима Дроздов"}
  ],
  "summary_replacements_orgs": [
    {"raw": "Шеффлер", "canonical": "Schaeffler"}
  ]
}
"""


def build_matcher_prompt(
    *,
    text: str,
    raw_owners: list[str],
    known_people: list[dict],
    known_orgs: list[dict],
    meeting_participants: list[str] | None = None,
) -> str:
    """Compose user prompt with all context для matcher LLM.

    meeting_participants — список real_name участников ЭТОЙ конкретной
    встречи (для SPEAKER FALLBACK при дейктических 'я'/'мне')."""
    parts: list[str] = []
    parts.append("ТЕКСТ ВСТРЕЧИ:\n" + (text or "(empty)"))
    if meeting_participants:
        parts.append("\n\nMEETING_PARTICIPANTS (участники этой встречи):")
        for p in meeting_participants:
            parts.append(f"  - {p}")
    if raw_owners:
        parts.append("\n\nRAW OWNER MENTIONS из задач:")
        for r in raw_owners:
            parts.append(f"  - {r}")
    if known_people:
        parts.append("\n\nKNOWN PEOPLE (TeamMember):")
        for p in known_people:
            line = f"  - real_name=«{p.get('real_name')}»"
            if p.get("role"):
                line += f" | role=«{p['role']}»"
            if p.get("notes"):
                line += f" | notes=«{p['notes']}»"
            if p.get("tg_username"):
                line += f" | tg=@{p['tg_username']}"
            parts.append(line)
    if known_orgs:
        parts.append("\n\nKNOWN ORGS (Counterparty):")
        for o in known_orgs:
            aliases = o.get("aliases") or []
            line = f"  - name=«{o.get('name')}»"
            if aliases:
                line += f" | aliases={aliases}"
            parts.append(line)
    parts.append("\n\nВерни JSON по контракту.")
    return "\n".join(parts)


def _safe_json_parse(text: str) -> dict | None:
    if not text:
        return None
    t = str(text).strip()
    if t.startswith("```"):
        t = t.lstrip("`").lstrip("json").strip()
        if t.endswith("```"):
            t = t[:-3].strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        return None


def match_people(
    *,
    text: str,
    raw_owners: list[str],
    known_people: list[dict],
    llm_backend: Any,
    model: str,
) -> dict:
    """FR-CR-05-193b — resolve raw owners + summary people mentions.

    Returns:
        {
          "task_owners": [{raw_owner, tm_real_name, reasoning}],
          "summary_replacements_people": [{raw, canonical}],
        }
    """
    if not known_people:
        return {"task_owners": [], "summary_replacements_people": []}

    if not raw_owners and not text:
        return {"task_owners": [], "summary_replacements_people": []}

    prompt = build_matcher_prompt(
        text=text, raw_owners=raw_owners,
        known_people=known_people, known_orgs=[],
    )
    log.info("entity_matcher_started",
             raw_owners=len(raw_owners), known_people=len(known_people))
    try:
        raw = llm_backend.complete_text(
            system_prompt=_SYSTEM_PROMPT, user_prompt=prompt,
            model=model, reasoning_effort="medium",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("entity_matcher_llm_error", error=str(e))
        return {
            "task_owners": [
                {"raw_owner": r, "tm_real_name": None,
                 "reasoning": "llm_error"} for r in raw_owners
            ],
            "summary_replacements_people": [],
        }

    parsed = _safe_json_parse(raw)
    if not parsed:
        log.warning("entity_matcher_json_error", raw_head=str(raw)[:200])
        return {
            "task_owners": [
                {"raw_owner": r, "tm_real_name": None,
                 "reasoning": "invalid_json"} for r in raw_owners
            ],
            "summary_replacements_people": [],
        }

    log.info("entity_matcher_done",
             task_owners=len(parsed.get("task_owners") or []),
             summary_replacements=len(parsed.get("summary_replacements_people") or []))

    return {
        "task_owners": parsed.get("task_owners") or [],
        "summary_replacements_people": parsed.get("summary_replacements_people") or [],
    }


def match_orgs(
    *,
    text: str,
    known_orgs: list[dict],
    llm_backend: Any,
    model: str,
) -> list[dict]:
    """FR-CR-05-193b — resolve orgs mentions.

    Returns: list[{raw, canonical}].
    """
    if not known_orgs or not text:
        return []

    prompt = build_matcher_prompt(
        text=text, raw_owners=[], known_people=[], known_orgs=known_orgs,
    )
    try:
        raw = llm_backend.complete_text(
            system_prompt=_SYSTEM_PROMPT, user_prompt=prompt,
            model=model, reasoning_effort="medium",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("entity_matcher_orgs_llm_error", error=str(e))
        return []

    parsed = _safe_json_parse(raw)
    if not parsed:
        return []
    # uniqueness by raw
    seen: set[str] = set()
    out: list[dict] = []
    for r in (parsed.get("summary_replacements_orgs") or []):
        key = (r.get("raw") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"raw": key, "canonical": r.get("canonical") or ""})
    return out


def match_entities(
    *,
    text: str,
    raw_owners: list[str],
    known_people: list[dict],
    known_orgs: list[dict],
    llm_backend: Any,
    model: str,
    meeting_participants: list[str] | None = None,
) -> dict:
    """Combined: people + orgs в одном LLM call (preferred).

    meeting_participants — для SPEAKER FALLBACK при 'я'/'мне' (FR-CR-05-193b-5+).
    """
    if not known_people and not known_orgs:
        return {
            "task_owners": [],
            "summary_replacements_people": [],
            "summary_replacements_orgs": [],
        }

    prompt = build_matcher_prompt(
        text=text, raw_owners=raw_owners,
        known_people=known_people, known_orgs=known_orgs,
        meeting_participants=meeting_participants,
    )
    try:
        raw = llm_backend.complete_text(
            system_prompt=_SYSTEM_PROMPT, user_prompt=prompt,
            model=model, reasoning_effort="medium",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("entity_matcher_combined_llm_error", error=str(e))
        return {
            "task_owners": [
                {"raw_owner": r, "tm_real_name": None,
                 "reasoning": "llm_error"} for r in raw_owners
            ],
            "summary_replacements_people": [],
            "summary_replacements_orgs": [],
        }
    parsed = _safe_json_parse(raw) or {}
    return {
        "task_owners": parsed.get("task_owners") or [],
        "summary_replacements_people": parsed.get("summary_replacements_people") or [],
        "summary_replacements_orgs": parsed.get("summary_replacements_orgs") or [],
    }
