"""FR-CR-05-193c-3 — ID-locked tests для LLM-rewrite варианта Step 3.

Альтернатива regex'овому apply_text_replacements. LLM переписывает
текст применяя mappings с грамматическим согласованием (падежи).
"""
from __future__ import annotations

from unittest.mock import MagicMock


def test_fr_cr_05_193c_3_rewrite_calls_llm_with_mappings() -> None:
    """rewrite_with_canonicals передаёт LLM текст + mappings, возвращает
    переписанный текст."""
    from app.services.entity_rewrite import rewrite_with_canonicals

    llm = MagicMock()
    llm.complete_text.return_value = (
        "Звонок с Дмитрием Седовым и Еленой Радионовой про Schaeffler."
    )
    text = "Звонок с Димой и Леной про шаффлера."
    result = rewrite_with_canonicals(
        text,
        people_replacements=[
            {"raw": "Дима", "canonical": "Дмитрий Седов"},
            {"raw": "Лена", "canonical": "Радионова Елена"},
        ],
        org_replacements=[
            {"raw": "шаффлер", "canonical": "Schaeffler"},
        ],
        llm_backend=llm, model="gpt-5.5",
    )
    assert "Дмитрием Седовым" in result
    assert "Еленой Радионовой" in result
    assert "Schaeffler" in result
    # Подтверждаем что LLM реально вызвалась
    assert llm.complete_text.call_count == 1


def test_fr_cr_05_193c_3_empty_replacements_returns_text_unchanged() -> None:
    """Если mappings пустые — возврат исходного текста без LLM вызова."""
    from app.services.entity_rewrite import rewrite_with_canonicals

    llm = MagicMock()
    text = "Исходный текст без замен."
    result = rewrite_with_canonicals(
        text, people_replacements=[], org_replacements=[],
        llm_backend=llm, model="gpt-5.5",
    )
    assert result == text
    assert llm.complete_text.call_count == 0


def test_fr_cr_05_193c_3_empty_text_returns_empty() -> None:
    """Пустой text — пустой output, LLM не зовётся."""
    from app.services.entity_rewrite import rewrite_with_canonicals

    llm = MagicMock()
    result = rewrite_with_canonicals(
        "", people_replacements=[{"raw": "x", "canonical": "y"}],
        org_replacements=[], llm_backend=llm, model="gpt-5.5",
    )
    assert result == ""
    assert llm.complete_text.call_count == 0


def test_fr_cr_05_193c_3_llm_error_falls_back_to_source() -> None:
    """LLM упала — fallback: вернуть исходный текст (не теряем данные)."""
    from app.services.entity_rewrite import rewrite_with_canonicals

    llm = MagicMock()
    llm.complete_text.side_effect = RuntimeError("api error")
    text = "Исходный текст с Димой."
    result = rewrite_with_canonicals(
        text,
        people_replacements=[{"raw": "Дима", "canonical": "Дмитрий Седов"}],
        org_replacements=[],
        llm_backend=llm, model="gpt-5.5",
    )
    assert result == text


def test_fr_cr_05_193c_3_length_anomaly_falls_back() -> None:
    """Если LLM выдала результат сильно короче/длиннее (>2x или <0.5x) —
    fallback (защита от галлюцинаций / cutoff)."""
    from app.services.entity_rewrite import rewrite_with_canonicals

    llm = MagicMock()
    # 10-char input, LLM возвращает 100-char — anomaly
    llm.complete_text.return_value = "x" * 100
    text = "Дима тут."  # 9 chars
    result = rewrite_with_canonicals(
        text,
        people_replacements=[{"raw": "Дима", "canonical": "Дмитрий Седов"}],
        org_replacements=[],
        llm_backend=llm, model="gpt-5.5",
    )
    # Длина out (100) > 2x длины src (9) → fallback
    assert result == text


def test_fr_cr_05_193c_3_prompt_includes_mappings(monkeypatch) -> None:
    """Проверка что user_prompt реально содержит mappings блоки —
    LLM не может корректно переписать без них."""
    from app.services.entity_rewrite import rewrite_with_canonicals

    captured: dict = {}

    def fake_complete_text(**kwargs):
        captured.update(kwargs)
        return "переписанный текст того же размера примерно"

    llm = MagicMock()
    llm.complete_text.side_effect = fake_complete_text

    rewrite_with_canonicals(
        "оригинальный текст того же размера примерно",
        people_replacements=[{"raw": "Дима", "canonical": "Дмитрий Седов"}],
        org_replacements=[{"raw": "шаффлер", "canonical": "Schaeffler"}],
        llm_backend=llm, model="gpt-5.5",
    )
    user = captured.get("user_prompt", "")
    assert "Дима" in user and "Дмитрий Седов" in user
    assert "шаффлер" in user and "Schaeffler" in user
    assert "MAPPINGS" in user
