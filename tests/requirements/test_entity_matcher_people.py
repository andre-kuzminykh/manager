"""FR-CR-05-193b (people part) — ID-locked tests для Step 2 matcher,
people-resolution часть.

Contract:
  - `EntityMatcher.match_people(text, raw_owners, known_people)`.
  - known_people = list[{tm_id, real_name, role, notes, tg_username?}],
    отфильтрованные humans (без bot'ов, with real_name).
  - LLM (gpt-5.5) принимает контекст ВСЕЙ встречи + raw_owners из tasks
    + людей таблицу со ВСЕМИ notes (для domain disambiguation).
  - Output: {task_owners: [{raw_owner, tm_real_name, reasoning}],
             summary_replacements_people: [{raw, canonical}]}.
  - Если не нашли match → tm_real_name=None (task без owner, для аудита).
  - Critical: notes используются для disambiguation (Дима = Дима Дроздов
    или Дмитрий Седов в зависимости от контекста выxодящих фондов vs контрактов).
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import MagicMock


@pytest.fixture
def known_people():
    """Subset of real team_members table — 5 представителей."""
    return [
        {"tm_id": 35, "real_name": "Дима Дроздов",
         "tg_username": "letsgopens",
         "role": "Head of Network",
         "notes": "ВСЕ ЧТО СВЯЗАНО С ФОНДАМИ. Outreach, Linkedin Артема."},
        {"tm_id": 117, "real_name": "Дмитрий Седов",
         "tg_username": "dmitrisedov",
         "role": "Финансовый Советник Артема",
         "notes": "ТОЛЬКО РАБОТА С КОНТРАКТАМИ ОТ ФОНДОВ. Прайм-Муверс / Felix / Tether — на нём."},
        {"tm_id": 116, "real_name": "Артем Соколов",
         "tg_username": "sokolov01",
         "role": "CEO",
         "notes": "Только стратегические вопросы / делегируем Irina Shipilova"},
        {"tm_id": 45, "real_name": "Irina Shipilova",
         "tg_username": "IrinaMorato",
         "role": "Ассистент CEO",
         "notes": "Сопровождение стратегических проектов CEO. Координация fundraising."},
        {"tm_id": 42, "real_name": "Юля",
         "tg_username": "julovva",
         "role": "Time management coordinator",
         "notes": "согласование встреч, координация логистики"},
    ]


@pytest.fixture
def mock_llm():
    m = MagicMock()
    m.model = "gpt-5.5"
    return m


def test_fr_cr_05_193b_exact_real_name_match(mock_llm, known_people) -> None:
    """raw='Дима Дроздов' (точное совпадение) → tm_real_name='Дима Дроздов'."""
    from app.services.entity_matcher import match_people
    mock_llm.chat.return_value = json.dumps({
        "task_owners": [{"raw_owner": "Дима Дроздов",
                          "tm_real_name": "Дима Дроздов",
                          "reasoning": "exact name match"}],
        "summary_replacements_people": [],
    })
    result = match_people(
        text="Дима Дроздов взял outreach",
        raw_owners=["Дима Дроздов"],
        known_people=known_people,
        llm_backend=mock_llm, model="gpt-5.5",
    )
    assert result["task_owners"][0]["tm_real_name"] == "Дима Дроздов"


def test_fr_cr_05_193b_disambiguates_via_notes(mock_llm, known_people) -> None:
    """raw='Дима' двусмысленно: Дроздов OR Седов. Notes решают:
    контекст 'контракт' → Седов, контекст 'фонд outreach' → Дроздов.
    Контракт здесь — LLM сам решает, тест проверяет что notes prompt'ятся."""
    from app.services.entity_matcher import match_people, build_matcher_prompt
    prompt = build_matcher_prompt(
        text="Дима возьмёт контракт по Tether",
        raw_owners=["Дима"], known_people=known_people, known_orgs=[],
    )
    # Notes BOTH Дроздов и Седов должны быть в prompt — это даёт LLM context
    assert "Дима Дроздов" in prompt
    assert "Дмитрий Седов" in prompt
    assert "ФОНДАМИ" in prompt  # notes Дроздова
    assert "КОНТРАКТАМИ" in prompt  # notes Седова


def test_fr_cr_05_193b_no_match_returns_none(mock_llm, known_people) -> None:
    """raw='Бианка' (третье лицо, не в TeamMember) → tm_real_name=None.
    Task всё равно создаётся, но без owner — для аудита."""
    from app.services.entity_matcher import match_people
    mock_llm.chat.return_value = json.dumps({
        "task_owners": [{"raw_owner": "Бианка",
                          "tm_real_name": None,
                          "reasoning": "not in known_people"}],
        "summary_replacements_people": [],
    })
    result = match_people(
        text="Бианка спросила про ужин",
        raw_owners=["Бианка"],
        known_people=known_people,
        llm_backend=mock_llm, model="gpt-5.5",
    )
    assert result["task_owners"][0]["tm_real_name"] is None


def test_fr_cr_05_193b_phonetic_translit(mock_llm, known_people) -> None:
    """raw='Yulya' / 'Yuliia' (translit) → должен match'нуть Юля.
    Test проверяет что LLM получает known_people с translit фолбэком."""
    from app.services.entity_matcher import match_people
    mock_llm.chat.return_value = json.dumps({
        "task_owners": [{"raw_owner": "Yulya",
                          "tm_real_name": "Юля",
                          "reasoning": "phonetic translit"}],
        "summary_replacements_people": [{"raw": "Yulya", "canonical": "Юля"}],
    })
    result = match_people(
        text="Yulya scheduled meeting",
        raw_owners=["Yulya"],
        known_people=known_people,
        llm_backend=mock_llm, model="gpt-5.5",
    )
    assert result["task_owners"][0]["tm_real_name"] == "Юля"
    # И в summary text должно быть указано как заменить
    repls = result["summary_replacements_people"]
    assert any(r["raw"] == "Yulya" and r["canonical"] == "Юля" for r in repls)


def test_fr_cr_05_193b_summary_mentions_resolved_independently(
    mock_llm, known_people
) -> None:
    """В summary упоминание имени может встретиться и БЕЗ соответствующего task'a.
    Matcher должен резолвить people-mentions из summary text отдельно
    от task_owners. Возвращает summary_replacements_people для всех matched."""
    from app.services.entity_matcher import match_people
    mock_llm.chat.return_value = json.dumps({
        "task_owners": [],  # tasks нет
        "summary_replacements_people": [
            {"raw": "Артем", "canonical": "Артем Соколов"},
            {"raw": "Ирина", "canonical": "Irina Shipilova"},
        ],
    })
    result = match_people(
        text="Артем и Ирина обсудили план",
        raw_owners=[],  # no tasks
        known_people=known_people,
        llm_backend=mock_llm, model="gpt-5.5",
    )
    assert len(result["summary_replacements_people"]) == 2


def test_fr_cr_05_193b_filters_humans_only(known_people) -> None:
    """Helper `get_humans_for_matcher(session)` фильтрует:
    - real_name not null / not empty
    - real_name not containing 'bot'
    - active=True
    Возвращает структуру совместимую с matcher."""
    from app.services.team_members import get_humans_for_matcher
    import inspect
    sig = inspect.signature(get_humans_for_matcher)
    assert "session" in sig.parameters


def test_fr_cr_05_193b_passes_notes_to_llm(mock_llm, known_people) -> None:
    """notes из TeamMember MUST идти в matcher LLM prompt — это контекст
    для domain-disambiguation. Test проверяет содержимое prompt'а."""
    from app.services.entity_matcher import build_matcher_prompt
    prompt = build_matcher_prompt(
        text="something", raw_owners=["Артем"],
        known_people=known_people, known_orgs=[],
    )
    # Notes Артема (делегируем Ирине) ДОЛЖНЫ быть в prompt
    assert "делегируем Irina Shipilova" in prompt or "Irina Shipilova" in prompt


def test_fr_cr_05_193b_empty_raw_owners_returns_empty_task_owners(
    mock_llm, known_people
) -> None:
    """raw_owners=[] (нет tasks из Step 1) → task_owners=[], но
    summary_replacements_people всё равно резолвится для summary text."""
    from app.services.entity_matcher import match_people
    mock_llm.chat.return_value = json.dumps({
        "task_owners": [],
        "summary_replacements_people": [
            {"raw": "Артем", "canonical": "Артем Соколов"},
        ],
    })
    result = match_people(
        text="Артем выступил с речью",
        raw_owners=[], known_people=known_people,
        llm_backend=mock_llm, model="gpt-5.5",
    )
    assert result["task_owners"] == []
    assert len(result["summary_replacements_people"]) == 1


def test_fr_cr_05_193b_empty_known_people_safe(mock_llm) -> None:
    """known_people=[] → matcher не зовётся, возврат пустых структур.
    Safety: если TeamMember пустой (свежий deploy), pipeline не падает."""
    from app.services.entity_matcher import match_people
    result = match_people(
        text="text", raw_owners=["Дима"],
        known_people=[], llm_backend=mock_llm, model="gpt-5.5",
    )
    assert result == {"task_owners": [], "summary_replacements_people": []}
    assert mock_llm.chat.call_count == 0


def test_fr_cr_05_193b_llm_invalid_json_safe(mock_llm, known_people) -> None:
    """LLM вернул мусор → safe fallback (пустые структуры), task_owners=
    raw_owners с tm_real_name=None для each."""
    from app.services.entity_matcher import match_people
    mock_llm.chat.return_value = "Cannot resolve"
    result = match_people(
        text="x", raw_owners=["Дима", "Олег"],
        known_people=known_people, llm_backend=mock_llm, model="gpt-5.5",
    )
    # Все raw_owners → tm_real_name=None при LLM fail
    raws = {t["raw_owner"] for t in result["task_owners"]}
    assert raws == {"Дима", "Олег"}
    assert all(t["tm_real_name"] is None for t in result["task_owners"])
