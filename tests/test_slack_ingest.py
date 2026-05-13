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
