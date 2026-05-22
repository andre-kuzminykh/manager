"""FR-CR-05-193b-7 — Tests for canonical name resolver.

LLM-based primary path + rule-based fallback. Покрывают prod bug:
meeting_participants приходят из Google Calendar как «Ирина Шипилова»,
а TeamMember.real_name = «Irina Shipilova» (английский). Без
нормализации scrub отвергал все задачи Ирины.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock


# === LLM-based path ===


def test_fr_cr_05_193b_7_llm_resolves_translit() -> None:
    """LLM canonicalizer: «Ирина Шипилова» (calendar form) →
    «Irina Shipilova» (TM canonical)."""
    from app.services.team_member_canonical import (
        canonicalize_participants_via_llm,
    )
    known_people = [{"real_name": "Irina Shipilova"}, {"real_name": "Артем Соколов"}]
    llm = MagicMock()
    llm.complete_text.return_value = json.dumps({
        "mappings": [
            {"raw": "Ирина Шипилова", "canonical": "Irina Shipilova"},
            {"raw": "Артем Соколов", "canonical": "Артем Соколов"},
        ],
    })
    result = canonicalize_participants_via_llm(
        ["Ирина Шипилова", "Артем Соколов"],
        known_people=known_people,
        llm_backend=llm, model="gpt-5.5",
    )
    assert "Irina Shipilova" in result
    assert "Артем Соколов" in result
    # Запрос к LLM сделан
    assert llm.complete_text.call_count == 1


def test_fr_cr_05_193b_7_llm_returns_null_for_unknown() -> None:
    """LLM возвращает canonical=null для имени, которого нет в known_people →
    passthrough raw имени (не теряем)."""
    from app.services.team_member_canonical import (
        canonicalize_participants_via_llm,
    )
    known_people = [{"real_name": "Irina Shipilova"}]
    llm = MagicMock()
    llm.complete_text.return_value = json.dumps({
        "mappings": [
            {"raw": "Незнакомый Человек", "canonical": None},
        ],
    })
    result = canonicalize_participants_via_llm(
        ["Незнакомый Человек"],
        known_people=known_people,
        llm_backend=llm, model="gpt-5.5",
    )
    # passthrough — оставляем raw как есть для STRICT scrub'a
    assert "Незнакомый Человек" in result


def test_fr_cr_05_193b_7_llm_fail_falls_back_to_rule_based() -> None:
    """LLM упала — используем rule-based транслитерацию."""
    from app.services.team_member_canonical import (
        canonicalize_participants_via_llm,
    )
    known_people = [{"real_name": "Irina Shipilova"}]
    llm = MagicMock()
    llm.complete_text.side_effect = RuntimeError("api error")
    result = canonicalize_participants_via_llm(
        ["Ирина Шипилова"],
        known_people=known_people,
        llm_backend=llm, model="gpt-5.5",
    )
    # rule-based транслитерация всё равно резолвит
    assert "Irina Shipilova" in result


def test_fr_cr_05_193b_7_empty_known_people_passthrough() -> None:
    """Если known_people пустой — LLM не зовётся, raw passthrough."""
    from app.services.team_member_canonical import (
        canonicalize_participants_via_llm,
    )
    llm = MagicMock()
    result = canonicalize_participants_via_llm(
        ["Ирина Шипилова"],
        known_people=[],
        llm_backend=llm, model="gpt-5.5",
    )
    assert result == ["Ирина Шипилова"]
    assert llm.complete_text.call_count == 0


def test_fr_cr_05_193b_7_dedupe_preserves_order() -> None:
    """LLM mapped двух разных raw в один canonical → dedupe сохраняя порядок."""
    from app.services.team_member_canonical import (
        canonicalize_participants_via_llm,
    )
    known_people = [{"real_name": "Irina Shipilova"}]
    llm = MagicMock()
    llm.complete_text.return_value = json.dumps({
        "mappings": [
            {"raw": "Ирина Шипилова", "canonical": "Irina Shipilova"},
            {"raw": "Шипилова Ирина", "canonical": "Irina Shipilova"},
        ],
    })
    result = canonicalize_participants_via_llm(
        ["Ирина Шипилова", "Шипилова Ирина"],
        known_people=known_people,
        llm_backend=llm, model="gpt-5.5",
    )
    assert result == ["Irina Shipilova"]


# === Rule-based fallback ===


def test_fr_cr_05_193b_7_rule_exact_match() -> None:
    """Точное совпадение — passthrough."""
    from app.services.team_member_canonical import canonical_real_name_rule_based
    result = canonical_real_name_rule_based(
        "Irina Shipilova",
        [{"real_name": "Irina Shipilova"}],
    )
    assert result == "Irina Shipilova"


def test_fr_cr_05_193b_7_rule_transliteration() -> None:
    """Кириллица ↔ латиница через транслитерацию."""
    from app.services.team_member_canonical import canonical_real_name_rule_based
    # ru → en
    assert canonical_real_name_rule_based(
        "Ирина Шипилова",
        [{"real_name": "Irina Shipilova"}],
    ) == "Irina Shipilova"
    # en → ru
    assert canonical_real_name_rule_based(
        "Artem Sokolov",
        [{"real_name": "Артем Соколов"}],
    ) == "Артем Соколов"


def test_fr_cr_05_193b_7_rule_swapped_order() -> None:
    """«Шипилова Ирина» (фамилия имя) → «Irina Shipilova»."""
    from app.services.team_member_canonical import canonical_real_name_rule_based
    result = canonical_real_name_rule_based(
        "Шипилова Ирина",
        [{"real_name": "Irina Shipilova"}],
    )
    assert result == "Irina Shipilova"


def test_fr_cr_05_193b_7_rule_case_insensitive() -> None:
    """Регистр игнорируется."""
    from app.services.team_member_canonical import canonical_real_name_rule_based
    result = canonical_real_name_rule_based(
        "irina shipilova",
        [{"real_name": "Irina Shipilova"}],
    )
    assert result == "Irina Shipilova"


def test_fr_cr_05_193b_7_rule_passthrough_when_unknown() -> None:
    """Неизвестное имя — passthrough как есть."""
    from app.services.team_member_canonical import canonical_real_name_rule_based
    result = canonical_real_name_rule_based(
        "Совсем Незнакомый",
        [{"real_name": "Irina Shipilova"}],
    )
    assert result == "Совсем Незнакомый"
