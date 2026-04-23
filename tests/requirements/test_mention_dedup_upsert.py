"""Regression: Slack sends both app_mention and message.channels for the same
message; the passive handler must skip mentions and upserts must be
idempotent under concurrent writes."""
from __future__ import annotations

from app.models import SlackConversation, SlackMessage
from app.slack_bot.handlers.events import _is_ignorable


# --------------------------------------------------------------------------- #
# Passive handler skips messages that mention the bot
# --------------------------------------------------------------------------- #


def test_is_ignorable_true_when_text_mentions_bot():
    assert (
        _is_ignorable({"text": "<@UBOT> make a task please"}, "UBOT") is True
    )


def test_is_ignorable_false_for_regular_message():
    assert (
        _is_ignorable({"text": "just a chat message"}, "UBOT") is False
    )


def test_is_ignorable_false_when_other_user_mentioned():
    assert (
        _is_ignorable({"text": "<@U999> fyi", "user": "U1"}, "UBOT") is False
    )


def test_is_ignorable_handles_no_bot_user_id():
    assert _is_ignorable({"text": "hello"}, None) is False


def test_handle_message_skips_mention_text(
    patched_session_scope, services_task, sender, ack, bolt_context, slack_client, SessionFactory
):
    from app.slack_bot.handlers.events import handle_message

    handle_message(
        event={
            "ts": "1.0",
            "user": "U1",
            "text": "<@UBOT> создай задачу",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "MsgSkipEv"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        # нет ни конвершейшена, ни записи сообщения, ни драфта
        assert s.query(SlackConversation).count() == 0
        assert s.query(SlackMessage).count() == 0


# --------------------------------------------------------------------------- #
# upsert_conversation is idempotent
# --------------------------------------------------------------------------- #


def test_upsert_conversation_returns_existing(session):
    from app.slack_bot.handlers.shared import upsert_conversation

    first = upsert_conversation(session, channel_id="C1", kind="channel")
    session.commit()
    second = upsert_conversation(session, channel_id="C1", kind="channel")
    assert first.id == second.id == "C1"
    assert session.query(SlackConversation).count() == 1


def test_upsert_conversation_handles_duplicate_insert_via_savepoint(session):
    """Simulate the race: pre-insert a row directly, then call upsert."""
    from app.slack_bot.handlers.shared import upsert_conversation

    # upsert via helper
    rec = upsert_conversation(session, channel_id="C9", kind="channel")
    assert rec.id == "C9"
    # second call with cache cleared to force .get() to query DB
    session.expire_all()
    again = upsert_conversation(session, channel_id="C9", kind="channel")
    assert again.id == "C9"
    assert session.query(SlackConversation).count() == 1


# --------------------------------------------------------------------------- #
# upsert_message is idempotent
# --------------------------------------------------------------------------- #


def test_upsert_message_dedupes_by_conversation_and_ts(session):
    from app.slack_bot.handlers.shared import upsert_conversation, upsert_message

    conv = upsert_conversation(session, channel_id="C1", kind="channel")
    m1 = upsert_message(session, conversation=conv, message={"ts": "9.9", "text": "x", "user": "U1"})
    session.expire_all()
    m2 = upsert_message(session, conversation=conv, message={"ts": "9.9", "text": "x", "user": "U1"})
    assert m1.id == m2.id
    assert session.query(SlackMessage).count() == 1
