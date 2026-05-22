"""FR-CR-05-193c-3 — LLM-rewrite вариант Step 3.

Альтернатива `apply_text_replacements` (regex single-pass). Берёт текст
+ списки mappings (raw→canonical) для людей и орг, передаёт LLM с
инструкцией ПЕРЕПИСАТЬ текст используя canonical-имена в правильных
падежах (согласовать с глаголами/прилагательными по-русски).

Зачем: regex-replace ломает падежи («Лене» → «Радионова Елена» вместо
«Радионовой Елене»; «Артема ждали» → «Артем Соколов ждали»). LLM пишет
грамотно.
"""
from __future__ import annotations

from typing import Any

from app.logging_setup import get_logger

log = get_logger(__name__)


_SYSTEM_PROMPT = """Ты редактор текста на русском языке. Тебе дан текст
встречи (саммари / описание задачи) и список mappings (raw → canonical)
для людей и организаций.

Задача — переписать текст СОХРАНЯЯ смысл, но заменив упоминания из raw
на canonical, СОГЛАСОВАВ грамматически:

  * имена в нужном падеже:
    «Лене» → «Елене Радионовой»  (не «Радионова Елена»)
    «Артема» → «Артема Соколова» (не «Артем Соколов»)
    «с Димой» → «с Дмитрием Седовым»
    «у Ольги» → «у Ольги Пономаренко»

  * организации тоже в нужном падеже:
    «звонок с шаффлером» → «звонок со Schaeffler»
    «приоритеты по тезору» → «приоритеты по Tezor»

  * НЕ дублируй фамилию если canonical уже состоит из имя+фамилия —
    «Артем Соколов» в тексте оставь как есть, не превращай в
    «Артем Соколов Соколов».

  * если raw НЕ встречается в тексте — игнорируй mapping.

  * если canonical уже стоит в тексте — не трогай.

  * orthography и пунктуация — точно как в оригинале, кроме замен.

  * структура (абзацы, переносы строк) — точно как в оригинале.

  * НЕ дописывай ничего своего, НЕ обобщай, НЕ сокращай.

Верни ТОЛЬКО переписанный текст, без префиксов / комментариев / JSON."""


def rewrite_with_canonicals(
    text: str | None,
    *,
    people_replacements: list[dict],
    org_replacements: list[dict],
    llm_backend: Any,
    model: str,
    reasoning_effort: str | None = "low",
) -> str:
    """Переписать text используя mappings, с правильными падежами.

    Args:
        text: исходный текст (summary_detailed / summary_short / task description)
        people_replacements: [{raw, canonical}, ...] для людей
        org_replacements: [{raw, canonical}, ...] для орг
        llm_backend: OpenAIBackend instance
        model: model name
        reasoning_effort: "low" by default (это не reasoning task)

    Returns:
        Переписанный text. Если LLM упала / пусто — возвращает исходный.
    """
    if not text or not text.strip():
        return text or ""
    all_repls = (people_replacements or []) + (org_replacements or [])
    if not all_repls:
        return text

    # Compose user prompt
    parts = ["ТЕКСТ:\n" + text, "\n\nMAPPINGS:"]
    if people_replacements:
        parts.append("\nЛюди:")
        for r in people_replacements:
            raw = (r.get("raw") or "").strip()
            canon = (r.get("canonical") or "").strip()
            if raw and canon:
                parts.append(f"  «{raw}» → «{canon}»")
    if org_replacements:
        parts.append("\nОрганизации:")
        for r in org_replacements:
            raw = (r.get("raw") or "").strip()
            canon = (r.get("canonical") or "").strip()
            if raw and canon:
                parts.append(f"  «{raw}» → «{canon}»")
    parts.append(
        "\n\nПерепиши текст с этими заменами, согласовав падежи."
    )
    user_prompt = "\n".join(parts)

    log.info(
        "entity_rewrite_started",
        text_chars=len(text),
        people_repls=len(people_replacements or []),
        org_repls=len(org_replacements or []),
    )
    try:
        result = llm_backend.complete_text(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model=model,
            reasoning_effort=reasoning_effort,
        ) or ""
    except Exception as e:  # noqa: BLE001
        log.warning("entity_rewrite_llm_error", error=str(e))
        return text

    result = result.strip()
    if not result:
        log.warning("entity_rewrite_empty_result", text_chars=len(text))
        return text

    # Защита: если LLM выдала результат сильно короче или ОЧЕНЬ
    # длиннее — скорее всего что-то сломалось (галлюцинация / cutoff).
    # Heuristic: длина должна быть в диапазоне 60-180% от исходной.
    src_len = len(text)
    out_len = len(result)
    if out_len < src_len * 0.5 or out_len > src_len * 2.0:
        log.warning(
            "entity_rewrite_length_anomaly",
            src_chars=src_len, out_chars=out_len,
            ratio=round(out_len / max(src_len, 1), 2),
        )
        return text

    log.info(
        "entity_rewrite_done",
        src_chars=src_len, out_chars=out_len,
        delta=out_len - src_len,
    )
    return result
