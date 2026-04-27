"""Tests for FR-6..FR-10 (explicit invocation: @mention, extraction,
confirmation, shortcuts, modal validation)."""
from __future__ import annotations

import json

import pytest
import yaml

from app.models import ActionDraft, ActionDraftState, ContextSnapshot, IntentInference
from app.slack_bot import blocks as bk
from app.slack_bot.handlers.shortcuts import (
    SHORTCUT_CREATE_MEETING,
    SHORTCUT_CREATE_TASK,
    handle_shortcut,
)


# =============================================================================
# FR-6: Explicit invoke via @mention.
# =============================================================================


def test_fr6_mention_flow_invocation_type_is_mention(
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
            "ts": "20.0",
            "user": "U1",
            "text": "<@UBOT> создай задачу: отчёт до пятницы",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Ment-a"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        inf = s.query(IntentInference).one()
        assert inf.invocation_type == "mention"


def test_fr6_mention_strips_bot_handle_from_stored_text(
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
            "ts": "21.0",
            "user": "U1",
            "text": "<@UBOT> создай встречу завтра",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Ment-b"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        snap = s.query(ContextSnapshot).one()
        assert "<@UBOT>" not in snap.source_message["text"]


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("<@UBOT> hi", "hi"),
        ("<@UBOT>   tabs", "tabs"),
        ("  <@UBOT> padded  ", "padded"),
        ("prefix <@UBOT> mid", "prefix  mid"),
        ("no mention here", "no mention here"),
    ],
)
def test_fr6_strip_bot_mentions_helper(raw, expected):
    from app.slack_bot.handlers.shared import strip_bot_mentions

    assert strip_bot_mentions(raw, "UBOT") == expected


def test_fr6_mention_posts_draft_when_classifier_returns_payload(
    patched_session_scope,
    services_task,
    sender,
    ack,
    bolt_context,
    slack_client,
):
    from app.slack_bot.handlers.events import handle_app_mention

    handle_app_mention(
        event={
            "ts": "22.0",
            "user": "U1",
            "text": "<@UBOT> create a task",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Ment-c"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=sender,
        ack=ack,
    )
    # CR-03: @mention auto-creates the task and posts a task-card (not a
    # draft widget) plus a follow-up question when fields are missing.
    assert len(sender.posted) >= 1
    first_block = sender.posted[0]["blocks"][0]
    assert first_block["type"] == "section"
    assert first_block["text"]["text"].startswith("*#")


def test_fr6_mention_falls_back_to_synthetic_draft_when_llm_silent(
    patched_session_scope,
    services_silent,
    sender,
    ack,
    bolt_context,
    slack_client,
):
    """Even when the classifier returns no_action, an explicit mention
    auto-creates a Task (CR-03) from the cleaned source text and posts
    a task-card + follow-up question."""
    from app.slack_bot.handlers.events import handle_app_mention

    handle_app_mention(
        event={
            "ts": "23.0",
            "user": "U1",
            "text": "<@UBOT> нифигась",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Ment-d"},
        client=slack_client,
        context=bolt_context,
        services=services_silent,
        sender=sender,
        ack=ack,
    )
    # Task-card first, then the follow-up question.
    assert len(sender.posted) >= 2
    first_block = sender.posted[0]["blocks"][0]
    assert first_block["type"] == "section"
    assert first_block["text"]["text"].startswith("*#")
    # And the first question is prefixed with :memo: Captured:.
    assert any("Captured" in m.get("text", "") for m in sender.posted)


# =============================================================================
# FR-7: Extraction of task/meeting structure from natural language.
# =============================================================================


def test_fr7_task_draft_schema_has_required_fields():
    from app.schemas.intent import TaskDraft

    fields = set(TaskDraft.model_fields.keys())
    assert {"title", "description", "owner_display_name", "priority", "due_date"}.issubset(fields)


def test_fr7_meeting_draft_schema_has_required_fields():
    from app.schemas.intent import MeetingDraft

    fields = set(MeetingDraft.model_fields.keys())
    assert {"title", "notes", "participants", "datetime_at", "timezone"}.issubset(fields)


def test_fr7_llm_tool_schema_declares_required_top_level_fields():
    from app.intent.classifier import _INTENT_TOOL

    required = _INTENT_TOOL["input_schema"]["required"]
    assert "intent" in required
    assert "confidence" in required


def test_fr7_priority_enum_is_closed():
    from app.intent.classifier import _INTENT_TOOL

    priority_enum = _INTENT_TOOL["input_schema"]["properties"]["task"]["properties"]["priority"][
        "enum"
    ]
    assert set(priority_enum) == {"low", "medium", "high", "urgent"}


def test_fr7_extraction_persisted_as_draft_payload(
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
            "ts": "30.0",
            "user": "U1",
            "text": "<@UBOT> создай задачу",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Extract-a"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        draft = s.query(ActionDraft).one()
        assert draft.payload["title"] == "Prepare report"
        assert draft.payload["priority"] == "high"


def test_fr7_parser_roundtrip_every_priority():
    from app.intent.classifier import _parse_classification

    for p in ("low", "medium", "high", "urgent"):
        out = _parse_classification(
            {
                "intent": "create_task",
                "confidence": 0.8,
                "task": {"title": "x", "priority": p},
            }
        )
        assert out.task.priority == p


# =============================================================================
# FR-8: User confirmation required before creating an entity.
# =============================================================================


def test_fr8_mention_confirms_draft_and_links_to_task_per_cr03(
    patched_session_scope,
    services_task,
    sender,
    ack,
    bolt_context,
    slack_client,
    SessionFactory,
):
    """CR-03 superseded FR-8 for the @mention path: the draft is auto-
    confirmed and links to a real Task. User confirmation is only
    required for the medium-confidence passive soft prompt flow."""
    from app.slack_bot.handlers.events import handle_app_mention

    handle_app_mention(
        event={
            "ts": "40.0",
            "user": "U1",
            "text": "<@UBOT> создай задачу: отчёт",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Conf-a"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        d = s.query(ActionDraft).one()
        assert d.state == ActionDraftState.confirmed
        assert d.task_id is not None


def test_fr8_mention_auto_creates_task_per_cr03(
    patched_session_scope,
    services_task,
    sender,
    ack,
    bolt_context,
    slack_client,
    SessionFactory,
):
    """CR-03 flip: explicit @mention materialises the task immediately."""
    from app.models import Meeting, Task
    from app.slack_bot.handlers.events import handle_app_mention

    handle_app_mention(
        event={
            "ts": "41.0",
            "user": "U1",
            "text": "<@UBOT> create task",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Conf-b"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        assert s.query(Task).count() == 1
        assert s.query(Meeting).count() == 0


def test_fr8_draft_card_has_three_buttons_in_order():
    from app.schemas.intent import IntentClassification, IntentType, TaskDraft

    c = IntentClassification(
        intent=IntentType.create_task, confidence=0.9, task=TaskDraft(title="x")
    )
    blocks = bk.draft_card(classification=c, draft_id=1, confidence_bucket="high")
    actions = next(b for b in blocks if b["type"] == "actions")
    button_ids = [el["action_id"] for el in actions["elements"]]
    assert button_ids == [bk.ACTION_CONFIRM, bk.ACTION_EDIT, bk.ACTION_IGNORE]


def test_fr8_ignore_action_marks_draft_ignored(
    patched_session_scope, SessionFactory, ack
):
    from app.models.intent import IntentType as IE
    from app.slack_bot.handlers.actions import handle_ignore
    from tests.test_persistence import _make_draft

    with SessionFactory() as s:
        d = _make_draft(s, intent=IE.create_task, payload={"title": "t"})
        s.commit()
        did = d.id

    handle_ignore(body={"actions": [{"value": str(did)}]}, ack=ack)

    with SessionFactory() as s:
        d = s.get(ActionDraft, did)
        assert d.state == ActionDraftState.ignored


def test_fr8_confirm_triggers_finalize(
    patched_session_scope, SessionFactory, ack, finalizer_stub, slack_client, sender
):
    from app.models.intent import IntentType as IE
    from app.slack_bot.handlers.actions import handle_confirm
    from tests.test_persistence import _make_draft

    with SessionFactory() as s:
        d = _make_draft(s, intent=IE.create_task, payload={"title": "x"})
        s.commit()
        did = d.id

    body = {
        "actions": [{"value": str(did)}],
        "channel": {"id": "C1"},
        "message": {
            "metadata": {
                "event_payload": {
                    "metadata": json.dumps(
                        {
                            "conversation_id": "C1",
                            "message_ts": "1.0",
                            "thread_ts": None,
                            "permalink": "https://x/y",
                            "context_snapshot_id": 1,
                            "source_user_id": "U1",
                        }
                    )
                }
            }
        },
    }
    handle_confirm(
        body=body,
        client=slack_client,
        services=None,
        finalizer=finalizer_stub,
        sender=sender,
        ack=ack,
    )
    assert len(finalizer_stub.calls) == 1
    assert finalizer_stub.calls[0][0] == did


# =============================================================================
# FR-9: At least 2 message shortcuts.
# =============================================================================


def test_fr9_shortcut_callback_ids_defined_and_distinct():
    assert SHORTCUT_CREATE_TASK != SHORTCUT_CREATE_MEETING
    assert SHORTCUT_CREATE_TASK.endswith("task_from_message")
    assert SHORTCUT_CREATE_MEETING.endswith("meeting_from_message")


def test_fr9_manifest_declares_both_shortcuts():
    with open("ops/slack-manifest.yaml", encoding="utf-8") as f:
        manifest = yaml.safe_load(f)
    callback_ids = {s["callback_id"] for s in manifest["features"]["shortcuts"]}
    assert {SHORTCUT_CREATE_TASK, SHORTCUT_CREATE_MEETING}.issubset(callback_ids)


@pytest.mark.parametrize("callback_id", [SHORTCUT_CREATE_TASK, SHORTCUT_CREATE_MEETING])
def test_fr9_each_shortcut_is_message_type(callback_id):
    with open("ops/slack-manifest.yaml", encoding="utf-8") as f:
        manifest = yaml.safe_load(f)
    entry = next(
        s for s in manifest["features"]["shortcuts"] if s["callback_id"] == callback_id
    )
    assert entry["type"] == "message"


# =============================================================================
# FR-10: Modal opens via views.open and validates required fields.
# =============================================================================


def test_fr10_task_shortcut_opens_task_modal(
    patched_session_scope, services_task, ack, slack_client, SessionFactory
):
    payload = {
        "callback_id": SHORTCUT_CREATE_TASK,
        "trigger_id": "trig-1",
        "channel": {"id": "C1"},
        "user": {"id": "U1"},
        "message": {"ts": "1.0", "user": "U1", "text": "надо собрать отчёт"},
    }
    handle_shortcut(shortcut=payload, client=slack_client, services=services_task, ack=ack)
    assert ack.called
    assert len(slack_client.views_opened) == 1
    assert slack_client.views_opened[0]["view"]["callback_id"] == bk.MODAL_CALLBACK_TASK


def test_fr10_meeting_shortcut_opens_meeting_modal(
    patched_session_scope, services_meeting, ack, slack_client, SessionFactory
):
    payload = {
        "callback_id": SHORTCUT_CREATE_MEETING,
        "trigger_id": "trig-2",
        "channel": {"id": "C1"},
        "user": {"id": "U1"},
        "message": {"ts": "1.0", "user": "U1", "text": "let's sync"},
    }
    handle_shortcut(shortcut=payload, client=slack_client, services=services_meeting, ack=ack)
    assert slack_client.views_opened[0]["view"]["callback_id"] == bk.MODAL_CALLBACK_MEETING


def test_fr10_missing_trigger_id_skips_views_open(
    patched_session_scope, services_task, ack, slack_client
):
    payload = {
        "callback_id": SHORTCUT_CREATE_TASK,
        "channel": {"id": "C1"},
        "user": {"id": "U1"},
        "message": {"ts": "1.0", "user": "U1", "text": "x"},
    }
    handle_shortcut(shortcut=payload, client=slack_client, services=services_task, ack=ack)
    assert slack_client.views_opened == []


def test_fr10_unknown_callback_id_is_noop(
    patched_session_scope, services_task, ack, slack_client
):
    payload = {
        "callback_id": "unknown_callback",
        "trigger_id": "t",
        "channel": {"id": "C1"},
        "user": {"id": "U1"},
        "message": {"ts": "1.0", "user": "U1", "text": "x"},
    }
    handle_shortcut(shortcut=payload, client=slack_client, services=services_task, ack=ack)
    assert slack_client.views_opened == []


def _ack_recorder():
    captured: dict = {}

    def ack(response_action=None, errors=None):
        captured["response_action"] = response_action
        captured["errors"] = errors or {}

    return ack, captured


def test_fr10_task_modal_rejects_empty_title(sender):
    from app.slack_bot.handlers.views import handle_task_modal_submit

    ack_fn, captured = _ack_recorder()
    view = {
        "state": {
            "values": {
                bk.BLOCK_TITLE: {bk.INPUT_TITLE: {"value": "  "}},
                bk.BLOCK_PRIORITY: {bk.INPUT_PRIORITY: {"selected_option": {"value": "low"}}},
            }
        },
        "private_metadata": "{}",
    }
    handle_task_modal_submit(
        body={}, view=view, services=None, finalizer=None, sender=sender, ack=ack_fn
    )
    assert bk.BLOCK_TITLE in captured["errors"]


def test_fr10_meeting_modal_rejects_missing_title_and_datetime(sender):
    from app.slack_bot.handlers.views import handle_meeting_modal_submit

    ack_fn, captured = _ack_recorder()
    view = {
        "state": {
            "values": {
                bk.BLOCK_TITLE: {bk.INPUT_TITLE: {"value": ""}},
                bk.BLOCK_PARTICIPANTS: {bk.INPUT_PARTICIPANTS: {"value": ""}},
                bk.BLOCK_DATETIME: {bk.INPUT_DATETIME: {"selected_date_time": None}},
                bk.BLOCK_NOTES: {bk.INPUT_NOTES: {"value": ""}},
            }
        },
        "private_metadata": "{}",
    }
    handle_meeting_modal_submit(
        body={}, view=view, services=None, finalizer=None, sender=sender, ack=ack_fn
    )
    assert bk.BLOCK_TITLE in captured["errors"]
    assert bk.BLOCK_DATETIME in captured["errors"]


def test_fr10_meeting_modal_accepts_valid_submission(
    patched_session_scope, SessionFactory, sender, finalizer_stub
):
    from app.slack_bot.handlers.views import handle_meeting_modal_submit

    ack_fn, captured = _ack_recorder()
    view = {
        "state": {
            "values": {
                bk.BLOCK_TITLE: {bk.INPUT_TITLE: {"value": "Sync"}},
                bk.BLOCK_PARTICIPANTS: {
                    bk.INPUT_PARTICIPANTS: {"value": "@alice, @bob"}
                },
                bk.BLOCK_DATETIME: {
                    bk.INPUT_DATETIME: {"selected_date_time": 1800000000}
                },
                bk.BLOCK_NOTES: {bk.INPUT_NOTES: {"value": "agenda"}},
            }
        },
        "private_metadata": "{}",
    }
    handle_meeting_modal_submit(
        body={}, view=view, services=None, finalizer=finalizer_stub, sender=sender, ack=ack_fn
    )
    # valid submission → ack with no errors
    assert not captured.get("errors")


def test_fr10_task_modal_has_correct_shape():
    view = bk.task_modal(private_metadata="{}", initial={"title": "x"})
    assert view["type"] == "modal"
    assert view["callback_id"] == bk.MODAL_CALLBACK_TASK
    title_block = next(b for b in view["blocks"] if b["block_id"] == bk.BLOCK_TITLE)
    assert "optional" not in title_block or title_block["optional"] is False


def test_fr10_meeting_modal_uses_datetimepicker():
    view = bk.meeting_modal(private_metadata="{}")
    dt_block = next(b for b in view["blocks"] if b["block_id"] == bk.BLOCK_DATETIME)
    assert dt_block["element"]["type"] == "datetimepicker"


def test_fr10_modal_private_metadata_roundtrips():
    pm = bk.task_modal(private_metadata='{"conversation_id":"C1"}')
    assert pm["private_metadata"] == '{"conversation_id":"C1"}'


@pytest.mark.parametrize(
    "initial_priority",
    ["low", "medium", "high", "urgent"],
)
def test_fr10_task_modal_prefills_priority(initial_priority):
    view = bk.task_modal(
        private_metadata="{}", initial={"title": "x", "priority": initial_priority}
    )
    priority_block = next(b for b in view["blocks"] if b["block_id"] == bk.BLOCK_PRIORITY)
    assert (
        priority_block["element"]["initial_option"]["value"] == initial_priority
    )
