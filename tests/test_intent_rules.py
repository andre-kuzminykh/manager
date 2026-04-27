from app.intent.rules import prefilter_intent
from app.schemas.intent import IntentType


def test_empty_text_returns_no_action():
    r = prefilter_intent("")
    assert r.hint == IntentType.no_action
    assert r.score == 0.0


def test_detects_russian_task_keyword():
    r = prefilter_intent("Сделай задачу — подготовить список фондов до пятницы")
    assert r.hint == IntentType.create_task
    assert r.score >= 0.5


def test_update_task_detected_over_create():
    r = prefilter_intent("Надо обновить задачу по отчёту, перенести дедлайн")
    assert r.hint == IntentType.update_task


def test_plain_chat_is_no_action():
    r = prefilter_intent("Всем привет! Как дела?")
    assert r.hint == IntentType.no_action
