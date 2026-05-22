"""FR-CR-05-193b-7 — Canonical name resolver via LLM (primary)
+ rule-based transliteration (fallback).

Резолвит свободную форму имени (например, `Ирина Шипилова` из
Google Calendar) к canonical `TeamMember.real_name` (например,
`Irina Shipilova`). Нужен потому что:

  - meeting_participants приходят из calendar_attendees где имена в
    форме которую вернул Google (русский display_name)
  - TeamMember.real_name — canonical (часто английский ASCII)

LLM matcher после Step 2 возвращает tm_real_name из known_people
(английский), а STRICT scrub сравнивает с meeting_participants
(русский) → ложное несовпадение → задачи всех Ирины/Оли scrubbed.

Решение — нормализовать meeting_participants к canonical TM real_name
ПЕРЕД передачей в matcher через LLM (более точно чем rule-based
транслитерация, потому что покрывает nicknames, аббревиатуры, и пр).
"""
from __future__ import annotations

import json
from typing import Any

from app.logging_setup import get_logger

log = get_logger(__name__)


# === Rule-based fallback (used when LLM fails / unavailable) ===

# Стандартная russian → english транслитерация (GOST 7.79-2000 упрощённая)
_RU_EN: dict[str, str] = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def transliterate_ru_to_en(text: str) -> str:
    """Russian Cyrillic → English ASCII (lowercase)."""
    if not text:
        return ""
    return "".join(_RU_EN.get(ch, ch) for ch in text.lower())


def _normalize(text: str) -> str:
    """Lowercase + transliterate + strip."""
    return transliterate_ru_to_en((text or "").strip())


def canonical_real_name_rule_based(
    raw_name: str,
    known_people: list[dict],
) -> str:
    """Rule-based fallback: exact / case-insensitive / transliteration /
    swap. Используется если LLM недоступна."""
    if not raw_name or not raw_name.strip():
        return raw_name
    raw = raw_name.strip()

    # 1. Exact
    for kp in known_people:
        rn = (kp.get("real_name") or "").strip()
        if rn == raw:
            return rn

    # 2. Case-insensitive
    raw_low = raw.lower()
    for kp in known_people:
        rn = (kp.get("real_name") or "").strip()
        if rn and rn.lower() == raw_low:
            return rn

    # 3. Transliteration обе стороны
    raw_translit = _normalize(raw)
    for kp in known_people:
        rn = (kp.get("real_name") or "").strip()
        if rn and _normalize(rn) == raw_translit:
            return rn

    # 4. Swapped first/last
    parts = raw.split()
    if len(parts) == 2:
        swapped_translit = _normalize(f"{parts[1]} {parts[0]}")
        for kp in known_people:
            rn = (kp.get("real_name") or "").strip()
            if rn and _normalize(rn) == swapped_translit:
                return rn

    return raw  # passthrough


# === LLM-based primary path ===

_SYSTEM_PROMPT = """Ты резолвер имён. Тебе дан список «raw» имён участников
встречи (как они пришли из Google Calendar) и список «known_people»
(canonical TeamMember real_name + опционально email).

Задача: для каждого raw-имени найти canonical real_name из known_people.
Учитывай:

  * EMAIL → real_name через email match: если raw это email
    (`user@domain.com`) и в known_people есть TeamMember с этим email —
    canonical = его real_name. Например `1@thehumanoid.ai` →
    «Артем Соколов» если у Артема email=1@thehumanoid.ai.
  * транслитерации обе стороны: «Ирина Шипилова» = «Irina Shipilova»
  * порядок частей: «Шипилова Ирина» = «Ирина Шипилова» = «Irina Shipilova»
  * nicknames / краткие формы: «Оля» = «Ольга Пономаренко», «Дима» = «Дмитрий Седов»
  * фонетические варианты: «Yulya» / «Yuliia» = «Юля»
  * NEVER инвентируй новые имена. Если raw не соответствует никому в
    known_people — поставь canonical=null.

Верни СТРОГО JSON:
{"mappings": [{"raw": "...", "canonical": "..." or null}, ...]}
"""


def canonicalize_participants_via_llm(
    raw_participants: list[str],
    *,
    known_people: list[dict],
    llm_backend: Any,
    model: str,
) -> list[str]:
    """LLM-based normalization. Для каждого raw_name выбирает canonical
    из known_people (с транслитерацией, nickname'ами и пр).

    Args:
        raw_participants: список свободных форм имён (из calendar)
        known_people: список dicts с `real_name`
        llm_backend: OpenAIBackend
        model: model name

    Returns:
        Список canonical real_names. Если LLM упала — fallback на
        rule-based (`canonical_real_name_rule_based`).
    """
    if not raw_participants:
        return []
    if not known_people:
        return list(raw_participants)

    # Compose prompt — добавляем email для каждого known_person (FR-CR-05-199c)
    raw_block = "\n".join(f"  - {r}" for r in raw_participants)
    known_lines: list[str] = []
    for kp in known_people:
        rn = kp.get("real_name")
        if not rn:
            continue
        email = kp.get("email")
        if email:
            known_lines.append(f"  - real_name=«{rn}» email=«{email}»")
        else:
            known_lines.append(f"  - real_name=«{rn}»")
    known_block = "\n".join(known_lines)
    user_prompt = (
        f"RAW PARTICIPANTS:\n{raw_block}\n\n"
        f"KNOWN_PEOPLE (canonical TeamMember real_name):\n{known_block}\n\n"
        f"Верни JSON {{mappings: [{{raw, canonical}}, ...]}}"
    )

    log.info(
        "participant_canonicalize_started",
        raw_count=len(raw_participants),
        known_count=len(known_people),
    )
    try:
        text = llm_backend.complete_text(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model=model,
            reasoning_effort="low",
            response_format={"type": "json_object"},
        ) or ""
    except Exception as e:  # noqa: BLE001
        log.warning("participant_canonicalize_llm_error", error=str(e))
        return [
            canonical_real_name_rule_based(r, known_people)
            for r in raw_participants
        ]

    try:
        data = json.loads(text) if text else {}
    except json.JSONDecodeError:
        log.warning(
            "participant_canonicalize_json_error",
            text_preview=text[:200],
        )
        return [
            canonical_real_name_rule_based(r, known_people)
            for r in raw_participants
        ]

    mappings = data.get("mappings") or []
    raw_to_canon: dict[str, str] = {}
    for m in mappings:
        if not isinstance(m, dict):
            continue
        raw = (m.get("raw") or "").strip()
        canon = (m.get("canonical") or "").strip() if m.get("canonical") else ""
        if raw:
            raw_to_canon[raw] = canon or raw  # passthrough если canonical=null

    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_participants:
        canonical = raw_to_canon.get(raw, raw)
        if canonical and canonical not in seen:
            seen.add(canonical)
            out.append(canonical)

    log.info(
        "participant_canonicalize_done",
        in_count=len(raw_participants),
        out_count=len(out),
        sample=[
            {"raw": r, "canonical": raw_to_canon.get(r)}
            for r in raw_participants[:5]
        ],
    )
    return out


# === Compatibility shims ===


def canonical_real_name(raw_name: str, known_people: list[dict]) -> str:
    """Rule-based вариант (без LLM). Backward-compat alias."""
    return canonical_real_name_rule_based(raw_name, known_people)


def canonicalize_participants(
    raw_participants: list[str],
    known_people: list[dict],
) -> list[str]:
    """Rule-based variant без LLM (для тестов / fallback)."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_participants:
        canonical = canonical_real_name_rule_based(raw, known_people)
        if canonical and canonical not in seen:
            seen.add(canonical)
            out.append(canonical)
    return out
