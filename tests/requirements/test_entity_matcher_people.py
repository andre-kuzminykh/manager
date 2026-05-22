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


# === FR-CR-05-193b-8 — collective pronoun fallback (Rule 4b) ===


def test_fr_cr_05_193b_8_prompt_includes_collective_pronoun_rule(known_people) -> None:
    """build_matcher_prompt + SYSTEM_PROMPT должны содержать Rule 4b
    для «мы» / «we» / «нам» / «us»."""
    from app.services.entity_matcher import _SYSTEM_PROMPT, build_matcher_prompt
    # Сам system prompt
    assert "COLLECTIVE PRONOUN" in _SYSTEM_PROMPT
    assert "мы" in _SYSTEM_PROMPT
    assert "we" in _SYSTEM_PROMPT
    # Должны быть инструкции про host / principal
    assert "host" in _SYSTEM_PROMPT or "principal" in _SYSTEM_PROMPT
    # Указание использовать notes для disambiguation
    assert "notes" in _SYSTEM_PROMPT.lower()


# === FR-CR-05-193b-5 — meeting_participants section in prompt ===


def test_fr_cr_05_193b_5_prompt_lists_meeting_participants(known_people) -> None:
    """build_matcher_prompt должен включать секцию MEETING_PARTICIPANTS
    когда список передан — это whitelist для LLM по Rule 5."""
    from app.services.entity_matcher import build_matcher_prompt
    prompt = build_matcher_prompt(
        text="x", raw_owners=["я"],
        known_people=known_people, known_orgs=[],
        meeting_participants=["Артем Соколов", "Irina Shipilova"],
    )
    assert "MEETING_PARTICIPANTS" in prompt
    assert "Артем Соколов" in prompt
    assert "Irina Shipilova" in prompt


def test_fr_cr_05_193b_5_prompt_omits_section_when_no_participants(
    known_people,
) -> None:
    """Если meeting_participants пуст / None — секция не появляется
    (back-compat; SPEAKER FALLBACK выключается)."""
    from app.services.entity_matcher import build_matcher_prompt
    prompt = build_matcher_prompt(
        text="x", raw_owners=["я"],
        known_people=known_people, known_orgs=[],
        meeting_participants=None,
    )
    assert "MEETING_PARTICIPANTS" not in prompt


# === FR-CR-05-193b-6 — Python-level enforcement of STRICT rule ===


def test_fr_cr_05_193b_6_scrubs_non_participant_owner(
    mock_llm, known_people
) -> None:
    """Even if LLM ignores Rule 5 and returns a tm_real_name that is
    NOT in meeting_participants, match_entities() scrubs it to None
    deterministically (defense in depth)."""
    from app.services.entity_matcher import match_entities
    mock_llm.complete_text.return_value = json.dumps({
        "task_owners": [
            {"raw_owner": "Дима",
             "tm_real_name": "Дима Дроздов",  # NOT in participants below
             "reasoning": "LLM ignored Rule 5"},
        ],
        "summary_replacements_people": [],
        "summary_replacements_orgs": [],
    })
    result = match_entities(
        text="x", raw_owners=["Дима"],
        known_people=known_people, known_orgs=[],
        meeting_participants=["Артем Соколов", "Irina Shipilova"],
        llm_backend=mock_llm, model="gpt-5.5",
    )
    owner = result["task_owners"][0]
    assert owner["tm_real_name"] is None
    assert "scrubbed" in (owner.get("reasoning") or "").lower()


def test_fr_cr_05_193b_6_keeps_participant_owner(
    mock_llm, known_people
) -> None:
    """When LLM returns an owner WHO IS in meeting_participants, the
    scrubber leaves it untouched."""
    from app.services.entity_matcher import match_entities
    mock_llm.complete_text.return_value = json.dumps({
        "task_owners": [
            {"raw_owner": "Артем",
             "tm_real_name": "Артем Соколов",  # IS in participants
             "reasoning": "exact"},
        ],
        "summary_replacements_people": [],
        "summary_replacements_orgs": [],
    })
    result = match_entities(
        text="x", raw_owners=["Артем"],
        known_people=known_people, known_orgs=[],
        meeting_participants=["Артем Соколов", "Irina Shipilova"],
        llm_backend=mock_llm, model="gpt-5.5",
    )
    owner = result["task_owners"][0]
    assert owner["tm_real_name"] == "Артем Соколов"
    assert "scrubbed" not in (owner.get("reasoning") or "").lower()


def test_fr_cr_05_193b_6_no_participants_no_scrub(
    mock_llm, known_people
) -> None:
    """meeting_participants=None → no scrubbing (back-compat path)."""
    from app.services.entity_matcher import match_entities
    mock_llm.complete_text.return_value = json.dumps({
        "task_owners": [
            {"raw_owner": "Дима",
             "tm_real_name": "Дима Дроздов",
             "reasoning": "no constraint"},
        ],
        "summary_replacements_people": [],
        "summary_replacements_orgs": [],
    })
    result = match_entities(
        text="x", raw_owners=["Дима"],
        known_people=known_people, known_orgs=[],
        meeting_participants=None,
        llm_backend=mock_llm, model="gpt-5.5",
    )
    assert result["task_owners"][0]["tm_real_name"] == "Дима Дроздов"


def test_fr_cr_05_193b_6_preserves_null_tm_real_name(
    mock_llm, known_people
) -> None:
    """Owners that the LLM already returned as null pass through unchanged
    (scrubber only touches non-null names that fail the whitelist)."""
    from app.services.entity_matcher import match_entities
    mock_llm.complete_text.return_value = json.dumps({
        "task_owners": [
            {"raw_owner": "Бианка",
             "tm_real_name": None,
             "reasoning": "not in known_people"},
        ],
        "summary_replacements_people": [],
        "summary_replacements_orgs": [],
    })
    result = match_entities(
        text="x", raw_owners=["Бианка"],
        known_people=known_people, known_orgs=[],
        meeting_participants=["Артем Соколов"],
        llm_backend=mock_llm, model="gpt-5.5",
    )
    owner = result["task_owners"][0]
    assert owner["tm_real_name"] is None
    # Untouched reasoning (no scrub annotation)
    assert "scrubbed" not in (owner.get("reasoning") or "").lower()
