"""Tests for FR-1..FR-5 (passive detection: ingestion, context, taxonomy,
confidence thresholds, logging of ambiguous cases)."""
from __future__ import annotations

import pytest
import yaml

from app.config import Settings
from app.context.retriever import ContextRetriever
from app.intent import prefilter_intent
from app.models import (
    ActionDraft,
    ContextSnapshot,
    IntentInference,
    SlackConversation,
    SlackMessage,
)
from app.models.intent import IntentType as IntentTypeEnum
from app.orchestrator import Orchestrator
from app.orchestrator.service import ConfidenceBucket, bucket_for
from app.schemas.intent import IntentClassification, IntentType, TaskDraft


# =============================================================================
# FR-1: Ingestion from MPIM / DM / channel where the bot participates.
# =============================================================================


@pytest.mark.parametrize(
    "channel_id, channel_type, expected_kind",
    [
        ("D1", "im", "im"),
        ("D9999", "im", "im"),
        ("G1", "mpim", "mpim"),
        ("G12345", "mpim", "mpim"),
        ("C1", "channel", "channel"),
        ("C9876", "channel", "channel"),
        ("G77", "group", "group"),  # private channel ids start with G
    ],
)
def test_fr1_ingests_messages_for_each_channel_kind(
    channel_id,
    channel_type,
    expected_kind,
    patched_session_scope,
    services,
    sender,
    ack,
    bolt_context,
    slack_client,
    SessionFactory,
):
    from app.slack_bot.handlers.events import handle_message

    handle_message(
        event={
            "ts": f"{channel_id}-1.0",
            "user": "U1",
            "text": "надо подготовить задачу",
            "channel": channel_id,
            "channel_type": channel_type,
        },
        body={"event_id": f"Ev-{channel_id}"},
        client=slack_client,
        context=bolt_context,
        services=services,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        conv = s.get(SlackConversation, channel_id)
        assert conv is not None
        assert conv.kind == expected_kind


@pytest.mark.parametrize(
    "subtype",
    ["message_changed", "message_deleted", "bot_message", "channel_join"],
)
def test_fr1_ignores_message_edits_deletes_and_joins(
    subtype,
    patched_session_scope,
    services,
    sender,
    ack,
    bolt_context,
    slack_client,
    SessionFactory,
):
    from app.slack_bot.handlers.events import handle_message

    handle_message(
        event={
            "ts": "1.0",
            "user": "U1",
            "text": "anything",
            "channel": "C1",
            "channel_type": "channel",
            "subtype": subtype,
        },
        body={"event_id": f"Ev-sub-{subtype}"},
        client=slack_client,
        context=bolt_context,
        services=services,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        assert s.query(SlackConversation).count() == 0


def test_fr1_ignores_messages_from_bot_itself(
    patched_session_scope, services, sender, ack, bolt_context, slack_client, SessionFactory
):
    from app.slack_bot.handlers.events import handle_message

    handle_message(
        event={
            "ts": "1.0",
            "user": bolt_context.bot_user_id,
            "text": "hi",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "EvBot1"},
        client=slack_client,
        context=bolt_context,
        services=services,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        assert s.query(SlackConversation).count() == 0


def test_fr1_ignores_empty_messages(
    patched_session_scope, services, sender, ack, bolt_context, slack_client, SessionFactory
):
    from app.slack_bot.handlers.events import handle_message

    handle_message(
        event={
            "ts": "1.0",
            "user": "U1",
            "text": "   ",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "EvEmpty"},
        client=slack_client,
        context=bolt_context,
        services=services,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        assert s.query(SlackConversation).count() == 0


def test_fr1_ingestion_persists_message_row(
    patched_session_scope, services, sender, ack, bolt_context, slack_client, SessionFactory
):
    from app.slack_bot.handlers.events import handle_message

    handle_message(
        event={
            "ts": "9.9",
            "user": "U1",
            "text": "сделай задачу",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Ev-persist"},
        client=slack_client,
        context=bolt_context,
        services=services,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        msg = s.query(SlackMessage).filter_by(ts="9.9").one()
        assert msg.conversation_id == "C1"
        assert msg.user_id == "U1"


def test_fr1_slack_manifest_subscribes_to_required_events():
    with open("ops/slack-manifest.yaml", encoding="utf-8") as f:
        manifest = yaml.safe_load(f)
    bot_events = set(manifest["settings"]["event_subscriptions"]["bot_events"])
    assert {"app_mention", "message.im", "message.mpim"}.issubset(bot_events)


def test_fr1_slack_manifest_requests_history_scopes():
    with open("ops/slack-manifest.yaml", encoding="utf-8") as f:
        manifest = yaml.safe_load(f)
    scopes = set(manifest["oauth_config"]["scopes"]["bot"])
    assert {"im:history", "mpim:history", "app_mentions:read", "chat:write"}.issubset(scopes)


def test_fr1_ingestion_does_not_post_when_classifier_silent(
    patched_session_scope,
    services_silent,
    sender,
    ack,
    bolt_context,
    slack_client,
):
    from app.slack_bot.handlers.events import handle_message

    handle_message(
        event={
            "ts": "7.7",
            "user": "U1",
            "text": "привет как дела",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Ev-silent"},
        client=slack_client,
        context=bolt_context,
        services=services_silent,
        sender=sender,
        ack=ack,
    )
    assert sender.posted == []


# =============================================================================
# FR-2: Context window = source + up to 10 previous + thread.
# =============================================================================


def _fake_client(history=None, replies=None):
    from tests.test_context_retriever import FakeClient

    return FakeClient(history_messages=history or [], replies_messages=replies or [])


@pytest.mark.parametrize("window_before", [1, 3, 5, 10, 20])
def test_fr2_window_before_respected(window_before):
    history = [{"ts": f"{i}.0", "user": "U1", "text": f"m{i}"} for i in range(50, 0, -1)]
    client = _fake_client(history=history)
    retriever = ContextRetriever(client, window_before=window_before)
    w = retriever.build(
        conversation_id="C1", source_message={"ts": "999.0", "user": "U1", "text": "s"}
    )
    assert len(w.history_before) == window_before


def test_fr2_window_default_is_ten():
    assert Settings().context_window_before == 10


def test_fr2_history_sorted_chronologically():
    history = [
        {"ts": "5.0", "user": "U1", "text": "e"},
        {"ts": "4.0", "user": "U2", "text": "d"},
        {"ts": "3.0", "user": "U2", "text": "c"},
        {"ts": "2.0", "user": "U1", "text": "b"},
        {"ts": "1.0", "user": "U3", "text": "a"},
    ]
    w = ContextRetriever(_fake_client(history=history), window_before=10).build(
        conversation_id="C1", source_message={"ts": "9.0", "user": "U1", "text": "src"}
    )
    assert [m["text"] for m in w.history_before] == ["a", "b", "c", "d", "e"]


def test_fr2_thread_replies_returned_when_thread_ts_present():
    replies = [
        {"ts": "10.0", "user": "U1", "text": "root"},
        {"ts": "10.1", "user": "U2", "text": "r1"},
        {"ts": "10.2", "user": "U3", "text": "r2"},
    ]
    w = ContextRetriever(_fake_client(replies=replies), window_before=5).build(
        conversation_id="C1",
        source_message={"ts": "10.1", "user": "U2", "text": "r1", "thread_ts": "10.0"},
    )
    assert len(w.thread_messages) == 3


def test_fr2_thread_not_called_without_thread_ts():
    client = _fake_client()
    retriever = ContextRetriever(client, window_before=5)
    retriever.build(
        conversation_id="C1", source_message={"ts": "1.0", "user": "U1", "text": "x"}
    )
    assert client.replies_calls == 0


def test_fr2_context_snapshot_dict_contains_all_parts():
    replies = [{"ts": "1.0", "user": "U1", "text": "root"}]
    history = [{"ts": "0.5", "user": "U2", "text": "prev"}]
    w = ContextRetriever(_fake_client(history=history, replies=replies), window_before=5).build(
        conversation_id="C1",
        source_message={"ts": "1.1", "user": "U2", "text": "reply", "thread_ts": "1.0"},
    )
    d = w.to_snapshot_dict()
    assert d["conversation_id"] == "C1"
    assert d["source_ts"] == "1.1"
    assert d["thread_ts"] == "1.0"
    assert d["history_before"]
    assert d["thread_messages"]
    assert d["source_message"]["text"] == "reply"


def test_fr2_flat_messages_does_not_duplicate_source_in_thread():
    replies = [
        {"ts": "1.0", "user": "U1", "text": "root"},
        {"ts": "1.1", "user": "U2", "text": "reply"},
    ]
    w = ContextRetriever(_fake_client(replies=replies), window_before=5).build(
        conversation_id="C1",
        source_message={"ts": "1.1", "user": "U2", "text": "reply", "thread_ts": "1.0"},
    )
    flat = w.flat_messages()
    assert sum(1 for m in flat if m["text"] == "reply") == 1


def test_fr2_history_missing_fields_are_normalized():
    history = [{"ts": "2.0", "user": "U1"}]  # no text
    w = ContextRetriever(_fake_client(history=history), window_before=5).build(
        conversation_id="C1", source_message={"ts": "3.0", "user": "U1", "text": "src"}
    )
    assert w.history_before[0]["text"] == ""


def test_fr2_history_failure_degrades_to_source_only():
    from slack_sdk.errors import SlackApiError

    class FailingClient:
        def conversations_history(self, **kwargs):
            raise SlackApiError(
                "rate limit", response=type("R", (), {"status_code": 429, "headers": {}})
            )

        def conversations_replies(self, **kwargs):
            return {"messages": []}

    retriever = ContextRetriever(FailingClient(), window_before=5)
    w = retriever.build(
        conversation_id="C1", source_message={"ts": "1.0", "user": "U1", "text": "x"}
    )
    assert w.history_before == []
    assert w.source_message["text"] == "x"


def test_fr2_handler_persists_context_snapshot_for_each_message(
    patched_session_scope, services, sender, ack, bolt_context, slack_client, SessionFactory
):
    from app.slack_bot.handlers.events import handle_message

    for i, ev in enumerate(["Ev-snap-1", "Ev-snap-2", "Ev-snap-3"]):
        handle_message(
            event={
                "ts": f"{i + 1}.0",
                "user": "U1",
                "text": "сделай задачу",
                "channel": "C1",
                "channel_type": "channel",
            },
            body={"event_id": ev},
            client=slack_client,
            context=bolt_context,
            services=services,
            sender=sender,
            ack=ack,
        )
    with SessionFactory() as s:
        assert s.query(ContextSnapshot).count() == 3


# =============================================================================
# FR-3: Intent taxonomy = {create_task, create_meeting, update_task,
#       update_meeting, no_action}
# =============================================================================


@pytest.mark.parametrize(
    "intent_name",
    ["create_task", "create_meeting", "update_task", "update_meeting", "no_action"],
)
def test_fr3_enum_supports_intent(intent_name):
    assert IntentType(intent_name)
    assert IntentTypeEnum(intent_name)


def test_fr3_enum_does_not_contain_unsupported():
    assert {v.value for v in IntentType} == {
        "create_task",
        "create_meeting",
        "update_task",
        "update_meeting",
        "no_action",
    }


def test_fr3_schema_validates_each_intent():
    for v in IntentType:
        c = IntentClassification(intent=v, confidence=0.5)
        assert c.intent == v


def test_fr3_llm_tool_schema_enum_values_match():
    from app.intent.classifier import _INTENT_TOOL

    enum_vals = set(_INTENT_TOOL["input_schema"]["properties"]["intent"]["enum"])
    assert enum_vals == {v.value for v in IntentType}


@pytest.mark.parametrize(
    "text, expected",
    [
        ("сделай задачу: собрать отчёт", IntentType.create_task),
        ("create a todo for tomorrow", IntentType.create_task),
        ("дедлайн до пятницы", IntentType.create_task),
        ("созвон завтра в 10", IntentType.create_meeting),
        ("let's sync next week", IntentType.create_meeting),
        ("meeting at 3pm", IntentType.create_meeting),
        ("перенеси задачу на среду", IntentType.update_task),
        ("update the task please", IntentType.update_task),
        ("перенеси встречу на среду", IntentType.update_meeting),
        ("reschedule meeting", IntentType.update_meeting),
        ("привет всем", IntentType.no_action),
        ("", IntentType.no_action),
    ],
)
def test_fr3_rules_prefilter_maps_text_to_intent(text, expected):
    assert prefilter_intent(text).hint == expected


def test_fr3_db_roundtrip_each_intent(session):
    from app.models import ActionDraft, IntentInference, ContextSnapshot
    from app.models.intent import ActionDraftState

    snap = ContextSnapshot(
        conversation_id="C1",
        source_ts="1.0",
        source_message={"ts": "1.0", "text": "x", "user": "U1"},
        history_before=[],
        thread_messages=[],
    )
    session.add(snap)
    session.flush()

    for intent in IntentTypeEnum:
        inf = IntentInference(
            context_snapshot_id=snap.id,
            intent=intent,
            confidence=0.5,
            invocation_type="passive",
        )
        session.add(inf)
        session.flush()
        d = ActionDraft(
            inference_id=inf.id,
            intent=intent,
            state=ActionDraftState.proposed,
            payload={},
        )
        session.add(d)
        session.flush()
        assert d.intent == intent


# =============================================================================
# FR-4: Configurable thresholds (low / medium / high).
# =============================================================================


@pytest.mark.parametrize(
    "conf, expected",
    [
        (0.99, ConfidenceBucket.high),
        (0.75, ConfidenceBucket.high),
        (0.74, ConfidenceBucket.medium),
        (0.4, ConfidenceBucket.medium),
        (0.3999, ConfidenceBucket.low),
        (0.0, ConfidenceBucket.low),
    ],
)
def test_fr4_bucket_boundaries_default(conf, expected):
    s = Settings(INTENT_CONFIDENCE_HIGH=0.75, INTENT_CONFIDENCE_LOW=0.4)
    assert bucket_for(conf, s) == expected


@pytest.mark.parametrize(
    "high, low",
    [
        (0.9, 0.5),
        (0.7, 0.3),
        (0.5, 0.25),
        (0.99, 0.95),
    ],
)
def test_fr4_custom_thresholds_reflected(high, low):
    s = Settings(INTENT_CONFIDENCE_HIGH=high, INTENT_CONFIDENCE_LOW=low)
    assert bucket_for(high + 1e-6, s) == ConfidenceBucket.high
    assert bucket_for((high + low) / 2, s) == ConfidenceBucket.medium
    assert bucket_for(max(low - 1e-6, 0.0), s) == ConfidenceBucket.low


def test_fr4_strict_thresholds_forces_soft_prompt_for_midrange():
    s = Settings(INTENT_CONFIDENCE_HIGH=0.99, INTENT_CONFIDENCE_LOW=0.2)
    c = IntentClassification(
        intent=IntentType.create_task, confidence=0.8, task=TaskDraft(title="x")
    )
    assert Orchestrator(s).decide_passive(classification=c, draft_id=1).action == "soft_prompt"


def test_fr4_lax_thresholds_trigger_card_for_midrange():
    s = Settings(INTENT_CONFIDENCE_HIGH=0.3, INTENT_CONFIDENCE_LOW=0.1)
    c = IntentClassification(
        intent=IntentType.create_task, confidence=0.35, task=TaskDraft(title="x")
    )
    assert Orchestrator(s).decide_passive(classification=c, draft_id=1).action == "card"


# =============================================================================
# FR-5: Logging / persistence of ambiguous cases for offline quality review.
# =============================================================================


def test_fr5_every_classification_persists_inference(
    patched_session_scope, services, sender, ack, bolt_context, slack_client, SessionFactory
):
    from app.slack_bot.handlers.events import handle_message

    handle_message(
        event={
            "ts": "10.0",
            "user": "U1",
            "text": "надо задачу сделать",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Ev-fr5-a"},
        client=slack_client,
        context=bolt_context,
        services=services,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        assert s.query(IntentInference).count() == 1


def test_fr5_inference_captures_raw_draft_payload(
    patched_session_scope,
    services_task,
    sender,
    ack,
    bolt_context,
    slack_client,
    SessionFactory,
):
    from app.slack_bot.handlers.events import handle_app_mention

    handle_app_mention(
        event={
            "ts": "11.0",
            "user": "U1",
            "text": "<@UBOT> сделай задачу",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Ev-fr5-b"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        inf = s.query(IntentInference).one()
        assert inf.raw["task"]["title"] == "Prepare report"


def test_fr5_inference_has_invocation_type(
    patched_session_scope,
    services,
    sender,
    ack,
    bolt_context,
    slack_client,
    SessionFactory,
):
    from app.slack_bot.handlers.events import handle_message

    handle_message(
        event={
            "ts": "12.0",
            "user": "U1",
            "text": "надо задачу",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Ev-fr5-c"},
        client=slack_client,
        context=bolt_context,
        services=services,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        inf = s.query(IntentInference).one()
        assert inf.invocation_type == "passive"


def test_fr5_reasoning_is_persisted_when_llm_provides_it(
    patched_session_scope,
    slack_client,
    sender,
    ack,
    bolt_context,
    SessionFactory,
):
    """When classifier returns a reasoning string, it is stored for review."""
    from app.schemas.intent import IntentClassification, IntentType, TaskDraft
    from tests.requirements.conftest import StubClassifier

    stub = StubClassifier(
        IntentClassification(
            intent=IntentType.create_task,
            confidence=0.55,
            reasoning="ambiguous: keyword + context uncertainty",
            task=TaskDraft(title="maybe"),
        )
    )

    from app.slack_bot.handlers.events import handle_message
    from tests.requirements.conftest import _make_services

    services = _make_services(slack_client, stub)
    handle_message(
        event={
            "ts": "13.0",
            "user": "U1",
            "text": "надо задачу?",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Ev-fr5-d"},
        client=slack_client,
        context=bolt_context,
        services=services,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        inf = s.query(IntentInference).one()
        assert "ambiguous" in (inf.reasoning or "")


def test_fr5_medium_confidence_logs_inference_even_when_no_card_shown(
    patched_session_scope,
    services_medium,
    sender,
    ack,
    bolt_context,
    slack_client,
    SessionFactory,
):
    from app.slack_bot.handlers.events import handle_message

    handle_message(
        event={
            "ts": "14.0",
            "user": "U1",
            "text": "возможно нужна задача",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Ev-fr5-e"},
        client=slack_client,
        context=bolt_context,
        services=services_medium,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        assert s.query(IntentInference).count() == 1


def test_fr5_silent_low_confidence_still_logs_inference_when_invoked_explicitly(
    patched_session_scope,
    services_silent,
    sender,
    ack,
    bolt_context,
    slack_client,
    SessionFactory,
):
    from app.slack_bot.handlers.events import handle_app_mention

    handle_app_mention(
        event={
            "ts": "15.0",
            "user": "U1",
            "text": "<@UBOT> something vague",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Ev-fr5-f"},
        client=slack_client,
        context=bolt_context,
        services=services_silent,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        assert s.query(IntentInference).count() == 1
