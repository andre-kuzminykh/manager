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

    # CR-03: task-card + follow-up question were posted.
    assert len(sender.posted) >= 2
    first_block = sender.posted[0]["blocks"][0]
    assert first_block["type"] == "section"
    assert first_block["text"]["text"].startswith("*#")

    with SessionFactory() as s:
        from app.models import Task

        drafts = s.query(ActionDraft).all()
        tasks = s.query(Task).all()
        assert len(drafts) == 1
        assert drafts[0].state == ActionDraftState.confirmed
        assert len(tasks) == 1
        assert tasks[0].title == "Надо сделать бота для сбора задач"


def test_mention_card_includes_recorded_title(
    patched_session_scope, services_silent, sender, ack, bolt_context, slack_client
):
    """FR-CR-05-63 — the «:memo: Captured: …» followup is gone
    (no more «when is this due?» nag). The recorded title still
    has to surface somewhere — it's on the live task card the
    bot posts in the source channel."""
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

    # The task card is posted somewhere in the channel — find it
    # by the title text. Slack blocks-format renders the title
    # inside the first `section` block.
    found = False
    for m in sender.posted:
        if "Допилить интеграцию" in (m.get("text") or ""):
            found = True
            break
        for blk in m.get("blocks") or []:
            text = (blk.get("text") or {}).get("text") or ""
            if "Допилить интеграцию" in text:
                found = True
                break
        if found:
            break
    assert found, sender.posted


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
    assert "don't see any text" in sender.posted[0]["text"].lower()


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
        # FR-CR-05-63 — `due_date` is no longer in the followup
        # field order (auto-defaults to today 18:00). With owner
        # falling back to author + title set from the cleaned
        # text, there's nothing left for the bot to ask: the
        # awaiting_field is None.
        assert d.awaiting_field is None
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
    # CR-03: first post is the real task-card — not a draft widget with
    # fields. Scan every block's text for the title.
    card_text = "\n".join(
        b.get("text", {}).get("text", "")
        for b in sender.posted[0]["blocks"]
        if b.get("type") == "section"
    )
    assert "Prepare report" in card_text
