"""FR-CR-05-193b (orgs part) — ID-locked tests для Step 2 matcher, orgs part."""
from __future__ import annotations

import json
import pytest
from unittest.mock import MagicMock


@pytest.fixture
def known_orgs():
    return [
        {"cp_id": 1, "name": "Schaeffler",
         "aliases": ["Шеффлер", "Шафлер", "Schaeffler AG"]},
        {"cp_id": 2, "name": "Bain Capital",
         "aliases": ["Bain", "Бэйн", "Bain Cap"]},
        {"cp_id": 3, "name": "Foundation Capital",
         "aliases": ["Foundation", "Фаундейшн"]},
        {"cp_id": 4, "name": "Khosla Ventures",
         "aliases": ["Khosla", "Хосла"]},
        {"cp_id": 5, "name": "Sequoia Capital",
         "aliases": ["Sequoia", "Секвойя"]},
    ]


@pytest.fixture
def mock_llm():
    m = MagicMock()
    m.model = "gpt-5.5"
    return m


def test_fr_cr_05_193b_orgs_exact_canonical(mock_llm, known_orgs) -> None:
    """raw='Schaeffler' (canonical) → canonical='Schaeffler' (no-op)."""
    from app.services.entity_matcher import match_orgs
    mock_llm.chat.return_value = json.dumps({
        "summary_replacements_orgs": [
            {"raw": "Schaeffler", "canonical": "Schaeffler"},
        ],
    })
    result = match_orgs(
        text="Обсудили с Schaeffler",
        known_orgs=known_orgs, llm_backend=mock_llm, model="gpt-5.5",
    )
    assert {"raw": "Schaeffler", "canonical": "Schaeffler"} in result


def test_fr_cr_05_193b_orgs_alias_match(mock_llm, known_orgs) -> None:
    """raw='Шеффлер' → matched через aliases → canonical='Schaeffler'."""
    from app.services.entity_matcher import match_orgs
    mock_llm.chat.return_value = json.dumps({
        "summary_replacements_orgs": [
            {"raw": "Шеффлер", "canonical": "Schaeffler"},
        ],
    })
    result = match_orgs(
        text="Шеффлер сделал предложение",
        known_orgs=known_orgs, llm_backend=mock_llm, model="gpt-5.5",
    )
    assert any(r["raw"] == "Шеффлер" and r["canonical"] == "Schaeffler"
               for r in result)


def test_fr_cr_05_193b_orgs_abbreviation_match(mock_llm, known_orgs) -> None:
    """raw='Bain' (abbreviation) → canonical='Bain Capital'."""
    from app.services.entity_matcher import match_orgs
    mock_llm.chat.return_value = json.dumps({
        "summary_replacements_orgs": [{"raw": "Bain", "canonical": "Bain Capital"}],
    })
    result = match_orgs(
        text="Bain decided to invest",
        known_orgs=known_orgs, llm_backend=mock_llm, model="gpt-5.5",
    )
    assert {"raw": "Bain", "canonical": "Bain Capital"} in result


def test_fr_cr_05_193b_orgs_cyrillic_latin(mock_llm, known_orgs) -> None:
    """Двусторонний переход: 'Хосла' → 'Khosla Ventures',
    'Khosla' → 'Khosla Ventures'."""
    from app.services.entity_matcher import match_orgs
    mock_llm.chat.return_value = json.dumps({
        "summary_replacements_orgs": [
            {"raw": "Хосла", "canonical": "Khosla Ventures"},
            {"raw": "Khosla", "canonical": "Khosla Ventures"},
        ],
    })
    result = match_orgs(
        text="Хосла и Khosla обсудили",
        known_orgs=known_orgs, llm_backend=mock_llm, model="gpt-5.5",
    )
    canons = [r["canonical"] for r in result]
    assert canons.count("Khosla Ventures") == 2


def test_fr_cr_05_193b_orgs_unknown_org_preserved(mock_llm, known_orgs) -> None:
    """Org НЕ в known_orgs → НЕ попадает в summary_replacements_orgs.
    LLM выдаёт unmatched. Текст остаётся как есть."""
    from app.services.entity_matcher import match_orgs
    mock_llm.chat.return_value = json.dumps({
        "summary_replacements_orgs": [],  # NewVCFund нет в known_orgs
        "unmatched_orgs": ["NewVCFund"],
    })
    result = match_orgs(
        text="NewVCFund — новый игрок",
        known_orgs=known_orgs, llm_backend=mock_llm, model="gpt-5.5",
    )
    assert result == []


def test_fr_cr_05_193b_orgs_empty_known_orgs_safe(mock_llm) -> None:
    """known_orgs=[] → LLM не зовётся, возврат []."""
    from app.services.entity_matcher import match_orgs
    result = match_orgs(
        text="Schaeffler",
        known_orgs=[], llm_backend=mock_llm, model="gpt-5.5",
    )
    assert result == []
    assert mock_llm.chat.call_count == 0


def test_fr_cr_05_193b_orgs_passes_aliases_to_prompt(known_orgs) -> None:
    """`build_matcher_prompt` инклюдит ВСЕ aliases каждой counterparty.
    Это критично для phonetic matching через LLM."""
    from app.services.entity_matcher import build_matcher_prompt
    prompt = build_matcher_prompt(
        text="x", raw_owners=[], known_people=[], known_orgs=known_orgs,
    )
    # Aliases Schaeffler ДОЛЖНЫ быть в prompt
    assert "Шеффлер" in prompt
    assert "Schaeffler AG" in prompt
    # Aliases Khosla
    assert "Хосла" in prompt


def test_fr_cr_05_193b_orgs_invalid_llm_json_safe(mock_llm, known_orgs) -> None:
    """LLM мусор → fallback пустой replacement list, текст не меняется."""
    from app.services.entity_matcher import match_orgs
    mock_llm.chat.return_value = "Sorry I cannot match"
    result = match_orgs(
        text="x", known_orgs=known_orgs,
        llm_backend=mock_llm, model="gpt-5.5",
    )
    assert result == []


def test_fr_cr_05_193b_orgs_partial_word_no_match(mock_llm, known_orgs) -> None:
    """Substring matches не должны соответствовать. 'Bainville' (substring 'Bain')
    НЕ должно match'нуть Bain Capital. LLM должна различать."""
    from app.services.entity_matcher import match_orgs
    mock_llm.chat.return_value = json.dumps({
        "summary_replacements_orgs": [],  # 'Bainville' — другая компания
    })
    result = match_orgs(
        text="Bainville Inc launches new product",
        known_orgs=known_orgs, llm_backend=mock_llm, model="gpt-5.5",
    )
    # Bainville не должен быть match'нут как Bain Capital
    assert not any(r["raw"] == "Bainville" for r in result)


def test_fr_cr_05_193b_orgs_multiple_occurrences_in_text(
    mock_llm, known_orgs
) -> None:
    """Один raw встречается несколько раз в тексте — matcher выдаёт
    одну запись в summary_replacements, apply (Step 3) применит ко всем
    вхождениям."""
    from app.services.entity_matcher import match_orgs
    mock_llm.chat.return_value = json.dumps({
        "summary_replacements_orgs": [
            {"raw": "Шеффлер", "canonical": "Schaeffler"},
        ],
    })
    result = match_orgs(
        text="Шеффлер хочет ... Шеффлер договорился ... Шеффлер сделал",
        known_orgs=known_orgs, llm_backend=mock_llm, model="gpt-5.5",
    )
    # Возврат — ОДНА replacement, не три (uniqueness)
    assert len(result) == 1
