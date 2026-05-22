"""FR-CR-05-193h — ID-locked tests для strict task.owner contract.

`task.owner_display_name` ОБЯЗАН быть либо canonical из TeamMember.real_name,
либо None. Никаких raw emails, никаких unmatched mentions, никаких raw
slack user IDs.
"""
from __future__ import annotations

from unittest.mock import MagicMock


def test_fr_cr_05_193h_no_raw_email_in_owner() -> None:
    """Если raw_owner_mention='sots@thehumanoid.ai' и нет совпадения по
    TeamMember.email — owner_display_name=None. Email НЕ попадает в БД
    как owner."""
    from app.services.entity_apply import apply_task_owner

    mock_session = MagicMock()
    mock_session.query().filter_by().first.return_value = None  # no TM match

    task = {"title": "x", "raw_owner_mention": "sots@thehumanoid.ai"}
    result = apply_task_owner(
        task, tm_real_name=None, session=mock_session,
    )
    assert result.get("owner_display_name") is None
    # Никакой email НЕ должен попасть в owner_display_name
    assert "@" not in (result.get("owner_display_name") or "")


def test_fr_cr_05_193h_owner_user_id_from_tm() -> None:
    """При matched TeamMember owner_user_id = slack_user_id (приоритет)
    или telegram_user_id (fallback)."""
    from app.services.entity_apply import apply_task_owner

    mock_session = MagicMock()
    tm = MagicMock(real_name="Дима Дроздов",
                   slack_user_id="U081HGB2ANS",
                   telegram_user_id=162194995,
                   notes="")
    mock_session.query().filter_by().first.return_value = tm
    task = {"title": "x", "raw_owner_mention": "Дима"}
    result = apply_task_owner(task, tm_real_name="Дима Дроздов",
                              session=mock_session)
    # slack_user_id выигрывает (если есть)
    assert result["owner_user_id"] == "U081HGB2ANS"


def test_fr_cr_05_193h_matcher_meta_stored() -> None:
    """`extra.matcher_meta` JSON со полями:
    {raw_owner_mention, tm_id, tm_real_name, reasoning, status}."""
    from app.services.entity_apply import apply_task_owner

    mock_session = MagicMock()
    tm = MagicMock(id=42, real_name="Дима Дроздов",
                   slack_user_id="U081", telegram_user_id=None, notes="")
    mock_session.query().filter_by().first.return_value = tm
    task = {"title": "x", "raw_owner_mention": "Дима"}
    result = apply_task_owner(
        task, tm_real_name="Дима Дроздов",
        matcher_reasoning="exact name match", session=mock_session,
    )
    meta = result.get("matcher_meta", {})
    assert meta["raw_owner_mention"] == "Дима"
    assert meta["tm_id"] == 42
    assert meta["tm_real_name"] == "Дима Дроздов"
    assert meta["status"] == "matched"
    assert "reasoning" in meta


def test_fr_cr_05_193h_default_state_proposed() -> None:
    """Tasks созданные через entity pipeline → state='proposed' (требует
    Confirm в TG). Кроме delegated case (там сразу 'todo')."""
    from app.services.entity_apply import build_task_from_extracted

    task_data = {
        "raw_owner_mention": "Дима",
        "title": "x",
        "description": "y",
        "owner_display_name": "Дима Дроздов",
        "owner_user_id": "U081",
        "matcher_meta": {"status": "matched"},
    }
    persisted = build_task_from_extracted(task_data, source_kind="zoom",
                                           source_conversation_id="z1")
    assert persisted["status"] == "proposed"


def test_fr_cr_05_193h_delegated_state_todo() -> None:
    """Delegated tasks → state='todo' (operator-pinned auto-confirm для
    delegate flow — FR-CR-05-192r)."""
    from app.services.entity_apply import build_task_from_extracted

    task_data = {
        "raw_owner_mention": "Артем",
        "title": "x",
        "owner_display_name": "Irina Shipilova",
        "owner_user_id": "U080",
        "matcher_meta": {"status": "delegated",
                          "original_owner": "Артем Соколов"},
    }
    persisted = build_task_from_extracted(task_data, source_kind="zoom",
                                           source_conversation_id="z1")
    assert persisted["status"] == "todo"


def test_fr_cr_05_193h_no_match_still_persists_task() -> None:
    """Task с unmatched owner ВСЁ РАВНО попадает в БД (для аудита).
    `owner_display_name=None`, `matcher_meta.status='no_match'`."""
    from app.services.entity_apply import build_task_from_extracted

    task_data = {
        "raw_owner_mention": "Бианка",
        "title": "Добавить ужин в календарь",
        "owner_display_name": None,
        "owner_user_id": None,
        "matcher_meta": {"status": "no_match",
                          "raw_owner_mention": "Бианка"},
    }
    persisted = build_task_from_extracted(task_data, source_kind="zoom",
                                           source_conversation_id="z1")
    assert persisted["title"] == "Добавить ужин в календарь"
    assert persisted["owner_display_name"] is None
    assert persisted["status"] == "proposed"


def test_fr_cr_05_193h_do_not_call_persists_with_status() -> None:
    """DO_NOT_CALL skip → task в БД с status='proposed',
    owner_display_name=None, matcher_meta.status='skipped_do_not_call'."""
    from app.services.entity_apply import build_task_from_extracted

    task_data = {
        "raw_owner_mention": "Елена",
        "title": "Юридический вопрос",
        "owner_display_name": None,
        "owner_user_id": None,
        "matcher_meta": {
            "status": "skipped_do_not_call",
            "original_owner": "Радионова Елена",
        },
    }
    persisted = build_task_from_extracted(task_data, source_kind="zoom",
                                           source_conversation_id="z1")
    assert persisted["owner_display_name"] is None
    assert persisted["extra"]["matcher_meta"]["status"] == "skipped_do_not_call"
    assert persisted["extra"]["matcher_meta"]["original_owner"] == "Радионова Елена"
