"""Follow-up flow: bot asks for missing fields, updates the card on reply."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.models import ActionDraft, ActionDraftState
from app.services.followup import parse_reply, pick_next_missing, prompt_for


# --------------------------------------------------------------------------- #
# pick_next_missing
# --------------------------------------------------------------------------- #


def test_pick_next_missing_for_task_asks_title_first():
    assert pick_next_missing("create_task", {}) == "title"


def test_pick_next_missing_for_task_asks_owner_after_title():
    # CR-04: owner comes before due_date so the passive draft-card path
    # and the @mention auto-create path ask the same first question.
    assert pick_next_missing("create_task", {"title": "x"}) == "owner"


def test_pick_next_missing_for_task_asks_due_after_owner_filled():
    assert (
        pick_next_missing(
            "create_task",
            {"title": "x", "owner_user_id": "U1", "owner_assumed": False},
        )
        == "due_date"
    )


def test_pick_next_missing_for_task_owner_filled_via_user_id():
    out = pick_next_missing(
        "create_task",
        {"title": "x", "due_date": "2026-05-01", "owner_user_id": "U1"},
    )
    assert out is None


def test_pick_next_missing_for_task_all_filled_returns_none():
    out = pick_next_missing(
        "create_task",
        {
            "title": "x",
            "due_date": "2026-05-01",
            "owner_user_id": "U1",
            "owner_display_name": "Ivan",
        },
    )
    assert out is None


def test_pick_next_missing_owner_unresolved_name_still_asks():
    """Bare display_name without a slack id means we couldn't match the
    user against ALLOWED_OWNERS; bot must keep asking instead of moving on."""
    out = pick_next_missing(
        "create_task",
        {
            "title": "x",
            "due_date": "2026-05-01",
            "owner_display_name": "Семен",  # not in allowed list
        },
    )
    assert out == "owner"


def test_pick_next_missing_for_meeting_order():
    assert pick_next_missing("create_meeting", {}) == "title"
    assert (
        pick_next_missing("create_meeting", {"title": "x"}) == "datetime_at"
    )
    assert (
        pick_next_missing(
            "create_meeting", {"title": "x", "datetime_at": "2026-06-01T10:00"}
        )
        == "participants"
    )
    assert (
        pick_next_missing(
            "create_meeting",
            {
                "title": "x",
                "datetime_at": "2026-06-01T10:00",
                "participants": ["@a"],
            },
        )
        is None
    )


# --------------------------------------------------------------------------- #
# parse_reply
# --------------------------------------------------------------------------- #


def test_parse_due_date_iso():
    out = parse_reply(
        field="due_date",
        reply_text="2026-05-01",
        settings=Settings(),
        today=date(2026, 4, 23),
    )
    assert out == {"due_date": "2026-05-01"}


def test_parse_due_date_relative_ru():
    out = parse_reply(
        field="due_date",
        reply_text="до пятницы",
        settings=Settings(),
        today=date(2026, 4, 23),  # четверг
    )
    assert out is not None
    # Next Friday from 2026-04-23 (Thursday) is 2026-04-24.
    assert out["due_date"].startswith("2026-04-")


def test_parse_due_date_unparseable_returns_none():
    out = parse_reply(
        field="due_date", reply_text="пойму когда будет", settings=Settings()
    )
    assert out is None


def test_parse_owner_resolves_against_allowed_list():
    s = Settings(ALLOWED_OWNERS='[{"slack_user_id":"U1","display_name":"Ivan"}]')
    out = parse_reply(field="owner", reply_text="Ivan", settings=s)
    assert out == {"owner_user_id": "U1", "owner_display_name": "Ivan"}


def test_parse_owner_unknown_returns_none():
    s = Settings(ALLOWED_OWNERS='[{"slack_user_id":"U1","display_name":"Ivan"}]')
    assert parse_reply(field="owner", reply_text="Boris", settings=s) is None


def test_parse_title_strips_and_returns():
    out = parse_reply(field="title", reply_text="  собрать отчёт ", settings=Settings())
    assert out == {"title": "собрать отчёт"}


def test_parse_title_strips_mentions():
    out = parse_reply(field="title", reply_text="<@UBOT> hello", settings=Settings())
    assert out == {"title": "hello"}


def test_parse_participants_split_on_comma():
    out = parse_reply(
        field="participants", reply_text="Ivan, @Anna, Boris", settings=Settings()
    )
    assert out == {"participants": ["Ivan", "@Anna", "Boris"]}


def test_parse_description_preserves_text():
    out = parse_reply(
        field="description", reply_text="все подробности тут", settings=Settings()
    )
    assert out == {"description": "все подробности тут"}


def test_parse_unknown_field_returns_none():
    assert parse_reply(field="whatever", reply_text="x", settings=Settings()) is None


def test_parse_empty_text_returns_none():
    assert parse_reply(field="due_date", reply_text="   ", settings=Settings()) is None


def test_prompt_for_contains_field_hint():
    assert "deadline" in prompt_for("due_date").lower()
    assert "participants" in prompt_for("participants").lower()


# --------------------------------------------------------------------------- #
# handle_message → follow-up branch
# --------------------------------------------------------------------------- #


def _make_draft(session, *, thread_ts, channel, field, payload, card_ts="100.0"):
    from app.models import ContextSnapshot, IntentInference
    from app.models.intent import IntentType as IE

    snap = ContextSnapshot(
        conversation_id=channel,
        source_ts=thread_ts,
        source_message={"ts": thread_ts, "text": "x", "user": "U1"},
        history_before=[],
        thread_messages=[],
    )
    session.add(snap)
    session.flush()
    inf = IntentInference(
        context_snapshot_id=snap.id,
        intent=IE.create_task,
        confidence=0.9,
        invocation_type="mention",
    )
    session.add(inf)
    session.flush()
    draft = ActionDraft(
        inference_id=inf.id,
        intent=IE.create_task,
        state=ActionDraftState.proposed,
        payload=payload,
        slack_message_ts=thread_ts,
        card_channel=channel,
        card_ts=card_ts,
        awaiting_field=field,
    )
    session.add(draft)
    session.flush()
    return draft


class _UpdatingSlackClient:
    def __init__(self):
        self.updated = []

    def chat_update(self, **kw):
        self.updated.append(kw)
        return SimpleNamespace(data={"ok": True, "ts": kw["ts"]})


def _make_sender_with_update(client):
    from app.slack_bot.rate_limiter import RateAwareSlackSender

    # RateAwareSlackSender just needs .chat_postMessage / .chat_update.
    class Cli:
        def __init__(inner):
            inner._update_client = client

        def chat_postMessage(inner, **kw):
            return SimpleNamespace(data={"ok": True, "ts": "0.0"})

        def chat_update(inner, **kw):
            return inner._update_client.chat_update(**kw)

    return RateAwareSlackSender(Cli(), min_interval_seconds=0.0)


def test_thread_reply_updates_due_date_and_card(
    patched_session_scope, SessionFactory, bolt_context, slack_client, ack
):
    from app.slack_bot.handlers.events import handle_message

    with SessionFactory() as s:
        draft = _make_draft(
            s,
            thread_ts="5.0",
            channel="C1",
            field="due_date",
            payload={"title": "собрать отчёт"},
        )
        s.commit()
        draft_id = draft.id

    updating_client = _UpdatingSlackClient()
    sender = _make_sender_with_update(updating_client)

    handle_message(
        event={
            "ts": "6.0",
            "thread_ts": "5.0",
            "user": "U1",
            "text": "2026-05-01",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "FollowEv1"},
        client=slack_client,
        context=bolt_context,
        services=_services_stub(slack_client),
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        d = s.get(ActionDraft, draft_id)
        assert d.payload["due_date"] == "2026-05-01"
        # owner still missing → awaiting_field rolls to "owner"
        assert d.awaiting_field == "owner"
    # Card was chat.update'd
    assert updating_client.updated
    assert updating_client.updated[0]["channel"] == "C1"
    assert updating_client.updated[0]["ts"] == "100.0"


def test_thread_reply_unparseable_gets_re_prompt(
    patched_session_scope, SessionFactory, bolt_context, slack_client, ack, sender
):
    from app.slack_bot.handlers.events import handle_message

    with SessionFactory() as s:
        draft = _make_draft(
            s,
            thread_ts="7.0",
            channel="C1",
            field="due_date",
            payload={"title": "x"},
        )
        s.commit()
        draft_id = draft.id

    handle_message(
        event={
            "ts": "7.5",
            "thread_ts": "7.0",
            "user": "U1",
            "text": "когда-нибудь",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "FollowEv2"},
        client=slack_client,
        context=bolt_context,
        services=_services_stub(slack_client),
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        d = s.get(ActionDraft, draft_id)
        # payload untouched, still awaiting due_date
        assert "due_date" not in d.payload
        assert d.awaiting_field == "due_date"
    assert any(
        "Couldn't parse" in m.get("text", "") for m in sender.posted
    )


def test_thread_reply_no_pending_draft_falls_through_to_passive(
    patched_session_scope,
    SessionFactory,
    bolt_context,
    slack_client,
    ack,
    sender,
    services_silent,
):
    """If there's no awaiting draft in the thread, the reply is just a normal
    passive message — silent classifier → nothing posted."""
    from app.slack_bot.handlers.events import handle_message

    handle_message(
        event={
            "ts": "8.0",
            "thread_ts": "7.0",
            "user": "U1",
            "text": "just chatting",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "FollowEv3"},
        client=slack_client,
        context=bolt_context,
        services=services_silent,
        sender=sender,
        ack=ack,
    )
    assert sender.posted == []


def _services_stub(slack_client):
    from tests.requirements.conftest import _make_services, StubClassifier

    return _make_services(slack_client, StubClassifier(auto=True))
