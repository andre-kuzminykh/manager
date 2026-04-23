"""Explicit @mention must ALWAYS produce a draft widget — even when the
classifier returns no_action on a short message. The bot should acknowledge
what it recorded and ask for the missing fields in the thread."""
from __future__ import annotations

from app.models import ActionDraft, ActionDraftState
from app.slack_bot.handlers.events import handle_app_mention


def test_mention_with_short_text_synthesises_task_draft(
    patched_session_scope,
    services_silent,  # classifier returns no_action
    sender,
    ack,
    bolt_context,
    slack_client,
    SessionFactory,
):
    handle_app_mention(
        event={
            "ts": "1.0",
            "user": "U1",
            "text": "<@UBOT> надо сделать бота для сбора задач",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "FallbackMent1"},
        client=slack_client,
        context=bolt_context,
        services=services_silent,
        sender=sender,
        ack=ack,
    )

    # Widget + follow-up question were posted
    assert len(sender.posted) >= 2
    first = sender.posted[0]
    assert first["blocks"][0]["text"]["text"] == "Task draft"

    with SessionFactory() as s:
        drafts = s.query(ActionDraft).all()
        assert len(drafts) == 1
        assert drafts[0].state == ActionDraftState.proposed
        assert drafts[0].payload["title"] == "надо сделать бота для сбора задач"


def test_mention_ack_message_includes_recorded_title(
    patched_session_scope, services_silent, sender, ack, bolt_context, slack_client
):
    handle_app_mention(
        event={
            "ts": "2.0",
            "user": "U1",
            "text": "<@UBOT> допилить интеграцию со стадиром",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "FallbackMent2"},
        client=slack_client,
        context=bolt_context,
        services=services_silent,
        sender=sender,
        ack=ack,
    )

    ack_msg = sender.posted[1]
    assert ":memo: Записал:" in ack_msg["text"]
    assert "допилить интеграцию" in ack_msg["text"]


def test_mention_without_text_still_replies(
    patched_session_scope, services_silent, sender, ack, bolt_context, slack_client
):
    handle_app_mention(
        event={
            "ts": "3.0",
            "user": "U1",
            "text": "<@UBOT>",  # nothing but the mention
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "FallbackMent3"},
        client=slack_client,
        context=bolt_context,
        services=services_silent,
        sender=sender,
        ack=ack,
    )

    assert len(sender.posted) == 1
    assert "не вижу текста" in sender.posted[0]["text"].lower()


def test_mention_fallback_has_awaiting_field_set(
    patched_session_scope,
    services_silent,
    sender,
    ack,
    bolt_context,
    slack_client,
    SessionFactory,
):
    handle_app_mention(
        event={
            "ts": "4.0",
            "user": "U1",
            "text": "<@UBOT> найти CTO",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "FallbackMent4"},
        client=slack_client,
        context=bolt_context,
        services=services_silent,
        sender=sender,
        ack=ack,
    )

    with SessionFactory() as s:
        d = s.query(ActionDraft).one()
        # With no due/owner known, the bot must queue a follow-up for the
        # next missing field.
        assert d.awaiting_field in ("due_date", "owner")
        assert d.card_channel == "C1"
        # card_ts is populated from Slack's post_message response; the
        # test RecordingSender omits ts, real Slack includes it.


def test_mention_when_classifier_succeeds_still_posts_full_flow(
    patched_session_scope, services_task, sender, ack, bolt_context, slack_client
):
    """Regression: the fallback must NOT fire when the LLM already produced a
    valid task draft — use the real classification."""
    handle_app_mention(
        event={
            "ts": "5.0",
            "user": "U1",
            "text": "<@UBOT> создай задачу",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "RealLLMMent"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=sender,
        ack=ack,
    )
    # First post is the card; fields from services_task fixture.
    card_fields = "\n".join(
        f["text"]
        for b in sender.posted[0]["blocks"]
        if b["type"] == "section" and "fields" in b
        for f in b["fields"]
    )
    assert "Prepare report" in card_fields
