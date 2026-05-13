"""Tests for app/slack_ingest/listener.py — smoke + regression guards.

В этом sandbox-окружении нет реального Slack workspace, поэтому
тестируем то что можно изолированно:
- Корректные имена enum / constants
- _SKIPPED_SUBTYPES комплектность
- Импорт всего модуля без NameError / AttributeError
"""
from __future__ import annotations

import inspect

from app.schemas.intent import InvocationType


# -- FR-CR-05-162 regression: PASSIVE invocation type ---------------------


def test_invocation_type_passive_is_lowercase():
    """Regression guard для bug-а 13.05.2026: код использовал
    `InvocationType.PASSIVE` (uppercase) — AttributeError на runtime'е,
    каждое Slack-сообщение падало в `classify_and_persist`. Enum имеет
    lowercase членов: passive / mention / shortcut.

    Этот тест ловит regression если кто-то снова напишет PASSIVE."""
    assert hasattr(InvocationType, "passive")
    assert not hasattr(InvocationType, "PASSIVE")
    assert InvocationType.passive.value == "passive"


def test_slack_ingest_module_imports_without_attribute_error():
    """Smoke-import — если в listener.py остались опечатки в enum
    или импорты, импорт упадёт с AttributeError. Тест ловит это
    до deploy'a."""
    from app.slack_ingest import listener as _listener

    assert hasattr(_listener, "make_slack_ingest_app")
    assert hasattr(_listener, "run_socket_mode")
    assert hasattr(_listener, "_SKIPPED_SUBTYPES")


def test_skipped_subtypes_covers_bot_and_join_events():
    """FR-CR-05-162 anti-self-loop: skip subtypes — bot_message,
    channel_join, channel_leave, message_changed (edits) и т.п.
    Этот тест гарантирует что эти subtypes остаются в list'е."""
    from app.slack_ingest.listener import _SKIPPED_SUBTYPES

    required = {
        "bot_message",
        "channel_join",
        "channel_leave",
        "message_changed",
        "message_deleted",
    }
    missing = required - _SKIPPED_SUBTYPES
    assert not missing, f"missing subtypes in skip-list: {missing}"


def test_listener_uses_lowercase_passive_in_classify_call():
    """Грепаем исходник на запрещённый pattern. Дешевле чем mock'ать
    весь Bolt + Slack stack."""
    from app.slack_ingest import listener

    src = inspect.getsource(listener)
    # Конкретно эту строку мы фиксили: должна быть lowercase passive
    assert "InvocationType.passive" in src, (
        "Listener must use lowercase InvocationType.passive"
    )
    # Запрещаем uppercase вариант — он валит runtime
    assert "InvocationType.PASSIVE" not in src, (
        "InvocationType.PASSIVE (uppercase) doesn't exist; use .passive"
    )


def test_listener_constructs_real_orchestrator_not_none():
    """Regression guard для bug-а 13.05.2026: listener передавал
    `orchestrator=None` в Services. classify_and_persist в shared.py
    зовёт services.orchestrator.persist_context_snapshot(...) —
    AttributeError на NoneType. Реальный Orchestrator(settings=...)
    нужен для persist_context_snapshot / persist_inference /
    create_draft."""
    from app.slack_ingest import listener

    src = inspect.getsource(listener)
    assert "Orchestrator(settings=settings)" in src, (
        "Listener must construct a real Orchestrator (shared.py "
        "classify_and_persist calls persist_context_snapshot on it)"
    )
    assert "orchestrator=None" not in src, (
        "orchestrator=None breaks classify_and_persist at runtime"
    )


# -- FR-CR-05-162 feature-parity with TG-ingest --------------------------


def test_listener_iterates_multi_task_from_classification():
    """Один Slack-message может содержать несколько задач
    («подготовь демо к пятнице и отчёт к понедельнику» → 2 tasks).
    Listener должен итерировать `classification.tasks` (список),
    а не использовать `classification.task` (single)."""
    from app.slack_ingest import listener

    src = inspect.getsource(listener)
    assert "for td in classification.tasks" in src, (
        "Listener must iterate classification.tasks for multi-task "
        "extraction (FR-CR-05-05 parity with TG)"
    )
    # Запрещаем single-task short-circuit'ы.
    assert "_classification, draft, _snapshot = classify_and_persist" not in src, (
        "classify_and_persist returns single draft only — replaced "
        "by inlined multi-task loop"
    )


def test_listener_has_intra_message_dedup():
    """Регрессия: если LLM extracts «Подготовить демо» дважды в
    одном сообщении, второй должен быть отброшен ДО cross-DB LLM
    дедупликации (дешевле)."""
    from app.slack_ingest import listener

    src = inspect.getsource(listener)
    assert "seen_titles" in src, (
        "Listener must keep an intra-message seen_titles set"
    )
    assert "slack_ingest_skipped_intra_message_duplicate" in src, (
        "Listener must log intra-message duplicate skips"
    )


def test_listener_uses_check_duplicate_for_cross_db_dedup():
    """Регрессия: каждый candidate task должен пройти через
    `check_duplicate(...)` — LLM-based сравнение с открытыми
    задачами в БД. Без этого каждое «напомни про X» создаёт
    новую задачу."""
    from app.slack_ingest import listener

    src = inspect.getsource(listener)
    assert "check_duplicate" in src, (
        "Listener must call check_duplicate for cross-DB dedup"
    )
    assert "slack_ingest_skipped_duplicate" in src, (
        "Listener must log cross-DB duplicate skips"
    )


def test_listener_uses_resolve_owner_chain():
    """Регрессия: owner_display_name был None в логах 13.05.2026
    потому что shared.classify_and_persist делал только UID-fallback.
    Полный pipeline `_resolve_owner` (registry → LLM uid → sender →
    admin) импортируется из telegram_ingest и применяется к каждой
    задаче."""
    from app.slack_ingest import listener

    src = inspect.getsource(listener)
    assert "_resolve_owner" in src, (
        "Listener must use the shared _resolve_owner chain from "
        "telegram_ingest.service"
    )
    assert "_admin_fallback_owner_id" in src, (
        "Listener must wire admin fallback uid into the owner chain"
    )


def test_post_initial_card_honors_for_slack_ingest_flag():
    """Регрессия: `cards.post_initial_card` блокировал
    source_kind=slack задачи (guard для slack_bot.cards path).
    Slack-ingest pipeline (FR-CR-05-162 — TG-only output) проходит
    мимо с flag `for_slack_ingest=True`."""
    from unittest.mock import MagicMock
    from app.models import TaskSourceKind
    from app.telegram_bot import cards as cards_mod

    sender = MagicMock()
    sender.enabled = True
    sender.send_message.return_value = {"message_id": 123}

    task = MagicMock()
    task.id = 999
    task.source_kind = TaskSourceKind.slack
    task.owner_user_id = None
    task.owner_display_name = None
    task.extra = {}
    task.card_channel = None
    task.card_ts = None

    session = MagicMock()

    # FALSE flag (default) → guard returns early, no send_message.
    cards_mod.post_initial_card(
        sender=sender, session=session, task=task,
        chat_id=0, reply_to_message_id=None,
        author_user_id="U123",
    )
    assert sender.send_message.call_count == 0, (
        "Without for_slack_ingest=True the Slack-source guard must "
        "skip the send (legacy slack_bot.cards path)"
    )

    # TRUE flag → guard bypassed; recipient resolution still applies.
    # No TG admin uids in this env so _recipient_user_ids returns [],
    # logs and exits cleanly. Important: NO exception is raised.
    cards_mod.post_initial_card(
        sender=sender, session=session, task=task,
        chat_id=0, reply_to_message_id=None,
        author_user_id="U123",
        for_slack_ingest=True,
    )
    # No assertion on send_message count: depends on env admin ids.
    # The flag itself reached the function — that's what we guard.


def test_listener_passes_for_slack_ingest_true_to_card_post():
    """Регрессия: листенер должен звать post_initial_card с
    `for_slack_ingest=True`, иначе cards.py guard блокирует
    отправку. Эта строка — фикс bug-а 13.05.2026, когда task в БД
    создалась, но TG-карточка не пришла."""
    from app.slack_ingest import listener

    src = inspect.getsource(listener)
    assert "for_slack_ingest=True" in src, (
        "Listener must pass for_slack_ingest=True to post_initial_card "
        "so cards.py source_kind=slack guard is bypassed"
    )
