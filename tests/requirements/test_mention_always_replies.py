"""Mention UX invariants:
- bot MUST reply to every @mention (draft card, hint, or error message);
- draft card highlights missing fields so user knows what to provide;
- success message includes a clickable source permalink.
"""
from __future__ import annotations

from datetime import date

from app.schemas.intent import (
    IntentClassification,
    IntentType,
    MeetingDraft,
    TaskDraft,
)
from app.slack_bot import blocks as bk
from app.slack_bot.handlers.events import _missing_fields, handle_app_mention


# --------------------------------------------------------------------------- #
# _missing_fields
# --------------------------------------------------------------------------- #


def test_missing_fields_flags_owner_and_due_for_bare_title():
    c = IntentClassification(
        intent=IntentType.create_task,
        confidence=0.9,
        task=TaskDraft(title="t"),
    )
    assert set(_missing_fields(c)) == {"owner", "due date"}


def test_missing_fields_empty_when_task_is_complete():
    c = IntentClassification(
        intent=IntentType.create_task,
        confidence=0.9,
        task=TaskDraft(
            title="t", owner_display_name="Ivan", due_date=date(2026, 5, 1)
        ),
    )
    assert _missing_fields(c) == []


def test_missing_fields_for_meeting_without_participants_or_datetime():
    c = IntentClassification(
        intent=IntentType.create_meeting,
        confidence=0.9,
        meeting=MeetingDraft(title="t"),
    )
    missing = _missing_fields(c)
    assert "participants" in missing
    assert "date/time" in missing


def test_missing_fields_for_no_action_is_empty():
    c = IntentClassification(intent=IntentType.no_action, confidence=0.1)
    assert _missing_fields(c) == []


# --------------------------------------------------------------------------- #
# draft_card renders the missing-fields hint
# --------------------------------------------------------------------------- #


def _context_texts(blocks):
    return [
        el["text"]
        for b in blocks
        if b["type"] == "context"
        for el in b["elements"]
    ]


def test_draft_card_shows_missing_hint_when_fields_empty():
    c = IntentClassification(
        intent=IntentType.create_task, confidence=0.9, task=TaskDraft(title="t")
    )
    blocks = bk.draft_card(
        classification=c,
        draft_id=1,
        confidence_bucket="high",
        missing_fields=["owner", "due date"],
    )
    ctx = "\n".join(_context_texts(blocks))
    assert "Не хватает" in ctx
    assert "owner" in ctx
    assert "due date" in ctx


def test_draft_card_shows_ready_hint_when_all_fields_present():
    c = IntentClassification(
        intent=IntentType.create_task,
        confidence=0.9,
        task=TaskDraft(title="t", owner_display_name="Ivan", due_date=date(2026, 5, 1)),
    )
    blocks = bk.draft_card(
        classification=c, draft_id=1, confidence_bucket="high", missing_fields=[]
    )
    ctx = "\n".join(_context_texts(blocks))
    assert "Все поля заполнены" in ctx


def test_draft_card_without_missing_fields_arg_behaves_like_empty():
    c = IntentClassification(
        intent=IntentType.create_task,
        confidence=0.9,
        task=TaskDraft(title="t", owner_display_name="Ivan", due_date=date(2026, 5, 1)),
    )
    blocks = bk.draft_card(classification=c, draft_id=1, confidence_bucket="high")
    ctx = "\n".join(_context_texts(blocks))
    assert "Не хватает" not in ctx


def test_draft_card_buttons_still_present_with_missing():
    c = IntentClassification(
        intent=IntentType.create_task, confidence=0.9, task=TaskDraft(title="t")
    )
    blocks = bk.draft_card(
        classification=c,
        draft_id=1,
        confidence_bucket="high",
        missing_fields=["owner"],
    )
    action_ids = [
        el["action_id"]
        for b in blocks
        if b["type"] == "actions"
        for el in b["elements"]
    ]
    assert action_ids == [bk.ACTION_CONFIRM, bk.ACTION_EDIT, bk.ACTION_IGNORE]


# --------------------------------------------------------------------------- #
# success_message includes the permalink
# --------------------------------------------------------------------------- #


def test_success_message_renders_permalink_link():
    blocks = bk.success_message(
        "task", 7, "demo", permalink="https://slack.com/archives/C1/p1"
    )
    text = blocks[0]["text"]["text"]
    assert "#7" in text
    assert "https://slack.com/archives/C1/p1" in text
    assert "Открыть исходное сообщение" in text


def test_success_message_without_permalink_omits_link():
    blocks = bk.success_message("task", 7, "demo")
    text = blocks[0]["text"]["text"]
    assert "Открыть исходное сообщение" not in text


# --------------------------------------------------------------------------- #
# handle_app_mention always replies
# --------------------------------------------------------------------------- #


def test_mention_replies_with_draft_card_when_intent_detected(
    patched_session_scope, services_task, sender, ack, bolt_context, slack_client
):
    handle_app_mention(
        event={
            "ts": "1.0",
            "user": "U1",
            "text": "<@UBOT> создай задачу",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "mra1"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=sender,
        ack=ack,
    )
    # Card is always the first post. A follow-up question for a missing
    # field (e.g. due_date) may follow in the thread.
    assert len(sender.posted) >= 1
    assert sender.posted[0].get("blocks")  # карточка


def test_mention_replies_with_widget_even_when_no_intent(
    patched_session_scope, services_silent, sender, ack, bolt_context, slack_client
):
    """Explicit mention → always a draft widget. The classifier may return
    no_action, but we still synthesise a minimal create_task from the text."""
    handle_app_mention(
        event={
            "ts": "2.0",
            "user": "U1",
            "text": "<@UBOT> просто привет",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "mra2"},
        client=slack_client,
        context=bolt_context,
        services=services_silent,
        sender=sender,
        ack=ack,
    )
    assert len(sender.posted) >= 1
    assert sender.posted[0].get("blocks")
    assert sender.posted[0]["blocks"][0]["text"]["text"] == "Task draft"


def test_mention_with_empty_text_asks_user_to_add_text(
    patched_session_scope, services_silent, sender, ack, bolt_context, slack_client
):
    """Only bare <@UBOT> with no content → polite 'add some text' reply."""
    handle_app_mention(
        event={
            "ts": "2.5",
            "user": "U1",
            "text": "<@UBOT>",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "mra2-bare"},
        client=slack_client,
        context=bolt_context,
        services=services_silent,
        sender=sender,
        ack=ack,
    )
    assert len(sender.posted) == 1
    text = sender.posted[0].get("text", "").lower()
    assert "не вижу текста" in text


def test_mention_replies_with_error_message_on_crash(
    patched_session_scope, sender, ack, bolt_context, slack_client, monkeypatch
):
    """If any exception bubbles up from classification, the bot must still
    post SOMETHING to the channel rather than going silent."""
    from app.slack_bot.handlers import events as events_module

    def boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(events_module, "classify_and_persist", boom)

    from tests.requirements.conftest import _make_services, StubClassifier

    services = _make_services(slack_client, StubClassifier(auto=True))
    handle_app_mention(
        event={
            "ts": "3.0",
            "user": "U1",
            "text": "<@UBOT> task",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "mra3"},
        client=slack_client,
        context=bolt_context,
        services=services,
        sender=sender,
        ack=ack,
    )
    assert len(sender.posted) == 1
    assert ":warning:" in sender.posted[0].get("text", "")


def test_mention_acks_even_on_crash(
    patched_session_scope, sender, bolt_context, slack_client, monkeypatch
):
    """ack() must still run before we attempt any work that could throw."""
    from app.slack_bot.handlers import events as events_module

    monkeypatch.setattr(
        events_module, "classify_and_persist", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )

    ack_calls = []

    def ack(*a, **kw):
        ack_calls.append(True)

    from tests.requirements.conftest import _make_services, StubClassifier

    services = _make_services(slack_client, StubClassifier(auto=True))
    handle_app_mention(
        event={
            "ts": "4.0",
            "user": "U1",
            "text": "<@UBOT> task",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "mra4"},
        client=slack_client,
        context=bolt_context,
        services=services,
        sender=sender,
        ack=ack,
    )
    assert ack_calls == [True]
