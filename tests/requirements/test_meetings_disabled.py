"""Requirement coverage: FR-CR-04-19 (meetings are out of scope).

The product is now task-only. We keep the legacy "Create meeting"
shortcut callback registered (so older Slack app manifests don't break)
and we keep the MeetingDraft schema + intent values (so historic rows
stay parseable), but the runtime never produces a meeting draft and
the meeting modal is never opened.
"""
from __future__ import annotations

from app.context.retriever import ContextWindow
from app.intent.classifier import classify_with_backend
from app.schemas.intent import InvocationType, IntentType
from app.slack_bot.handlers.shortcuts import (
    SHORTCUT_CREATE_MEETING,
    SHORTCUT_CREATE_TASK,
    handle_shortcut,
)


class _Backend:
    """Pipeline-aware stub matching the prefilter test's shape."""

    def __init__(self, *, detect=None):
        from app.intent.detect_prompt import DETECT_TOOL_NAME

        self._payloads = {DETECT_TOOL_NAME: detect}

    def extract_intent(self, *, user_prompt):  # pragma: no cover
        raise NotImplementedError

    def call_tool(self, **kw):
        return self._payloads.get(kw.get("tool_name"))


def _ctx(text: str) -> ContextWindow:
    return ContextWindow(
        conversation_id="C1",
        source_ts="1.0",
        thread_ts=None,
        source_message={"ts": "1.0", "user": "U-author", "text": text},
    )


# --------------------------------------------------------------------------- #
# Classifier never synthesises a meeting draft
# --------------------------------------------------------------------------- #


def test_meeting_keywords_stay_no_action_when_llm_says_no_action():
    backend = _Backend(detect={"is_task": False, "confidence": 0.1})
    out = classify_with_backend(
        backend=backend,
        context=_ctx("давайте созвон завтра в 11"),
        invocation_type=InvocationType.passive,
        source_text="давайте созвон завтра в 11",
    )
    assert out.intent == IntentType.no_action
    assert out.meeting is None


def test_meeting_keywords_in_english_stay_no_action():
    backend = _Backend(detect={"is_task": False, "confidence": 0.1})
    out = classify_with_backend(
        backend=backend,
        context=_ctx("let's have a meeting tomorrow"),
        invocation_type=InvocationType.passive,
        source_text="let's have a meeting tomorrow",
    )
    assert out.intent == IntentType.no_action
    assert out.meeting is None


# --------------------------------------------------------------------------- #
# Meeting shortcut shows a tasks-only ephemeral notice
# --------------------------------------------------------------------------- #


def test_meeting_shortcut_does_not_open_a_modal(
    patched_session_scope, services_meeting, ack, slack_client
):
    handle_shortcut(
        shortcut={
            "callback_id": SHORTCUT_CREATE_MEETING,
            "trigger_id": "trig",
            "channel": {"id": "C1"},
            "user": {"id": "U1"},
            "message": {"ts": "1.0", "user": "U1", "text": "sync?"},
        },
        client=slack_client,
        services=services_meeting,
        ack=ack,
    )
    assert slack_client.views_opened == []


def test_meeting_shortcut_posts_ephemeral_notice(
    patched_session_scope, services_meeting, ack, slack_client
):
    handle_shortcut(
        shortcut={
            "callback_id": SHORTCUT_CREATE_MEETING,
            "trigger_id": "trig",
            "channel": {"id": "C1"},
            "user": {"id": "U1"},
            "message": {"ts": "1.0", "user": "U1", "text": "sync?"},
        },
        client=slack_client,
        services=services_meeting,
        ack=ack,
    )
    assert slack_client.posted_ephemerals
    notice = slack_client.posted_ephemerals[0]
    assert notice["channel"] == "C1"
    assert notice["user"] == "U1"
    assert "task" in notice["text"].lower()


def test_task_shortcut_still_opens_task_modal(
    patched_session_scope, services_task, ack, slack_client
):
    """Sanity: disabling meetings must not regress the task shortcut."""
    from app.slack_bot import blocks as bk

    handle_shortcut(
        shortcut={
            "callback_id": SHORTCUT_CREATE_TASK,
            "trigger_id": "trig",
            "channel": {"id": "C1"},
            "user": {"id": "U1"},
            "message": {"ts": "1.0", "user": "U1", "text": "todo"},
        },
        client=slack_client,
        services=services_task,
        ack=ack,
    )
    assert slack_client.posted_ephemerals == []
    assert len(slack_client.views_opened) == 1
    assert (
        slack_client.views_opened[0]["view"]["callback_id"]
        == bk.MODAL_CALLBACK_TASK
    )
