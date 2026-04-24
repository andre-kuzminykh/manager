"""Owner detection runs as a SEPARATE LLM call with a focused prompt.
The single-shot classifier call handles intent/title/description/priority;
the follow-up owner call handles ONLY the assignee, with the conversation
context available to it."""
from __future__ import annotations

from datetime import date

from app.context.retriever import ContextWindow
from app.intent.classifier import classify_with_backend
from app.intent.owner_prompt import (
    OWNER_SYSTEM_PROMPT,
    OWNER_TOOL_NAME,
    build_owner_user_prompt,
)
from app.schemas.intent import InvocationType


class _RecordingBackend:
    """Records both calls so we can assert separation + prompt contents."""

    def __init__(self, *, intent_payload, owner_payload):
        self._intent = intent_payload
        self._owner = owner_payload
        self.calls: list[dict] = []

    def extract_intent(self, *, user_prompt):
        self.calls.append(
            {"kind": "intent", "user_prompt": user_prompt, "system_prompt": None}
        )
        return self._intent

    def call_tool(
        self,
        *,
        system_prompt,
        user_prompt,
        tool_name,
        tool_description,
        tool_parameters,
    ):
        self.calls.append(
            {
                "kind": tool_name,
                "user_prompt": user_prompt,
                "system_prompt": system_prompt,
            }
        )
        return self._owner


def _ctx(source_text, author="U-author", history=()):
    return ContextWindow(
        conversation_id="C1",
        source_ts="1.0",
        thread_ts=None,
        source_message={"ts": "1.0", "user": author, "text": source_text},
        history_before=list(history),
    )


def test_owner_call_uses_dedicated_system_prompt():
    backend = _RecordingBackend(
        intent_payload={
            "intent": "create_task",
            "confidence": 0.9,
            "task": {"title": "собрать демо"},
        },
        owner_payload={"reasoning": "no assignee", "display_name": None},
    )
    classify_with_backend(
        backend=backend,
        context=_ctx("надо собрать демо"),
        invocation_type=InvocationType.mention,
        source_text="надо собрать демо",
    )
    owner_call = next(c for c in backend.calls if c["kind"] == OWNER_TOOL_NAME)
    # Owner system prompt is different from the main one — it knows only
    # about assignees, nothing about tasks or dates.
    assert owner_call["system_prompt"] == OWNER_SYSTEM_PROMPT
    assert "ASSIGNEE" in owner_call["system_prompt"]
    assert "intent" not in owner_call["system_prompt"].lower()


def test_owner_call_fills_slack_user_id_when_returned():
    backend = _RecordingBackend(
        intent_payload={
            "intent": "create_task",
            "confidence": 0.9,
            "task": {"title": "собрать демо"},
        },
        owner_payload={
            "slack_user_id": "U-ivan",
            "display_name": "Иван",
            "reasoning": "message addresses <@U-ivan>",
        },
    )
    out = classify_with_backend(
        backend=backend,
        context=_ctx("<@U-ivan> сделай демо"),
        invocation_type=InvocationType.mention,
        source_text="<@U-ivan> сделай демо",
    )
    assert out.task.owner_user_id == "U-ivan"
    assert out.task.owner_display_name == "Иван"


def test_owner_call_clears_main_pass_guess_when_focused_call_says_no():
    """If the main classifier hallucinated an owner, the focused call
    (which says "nobody") must override and clear it."""
    backend = _RecordingBackend(
        intent_payload={
            "intent": "create_task",
            "confidence": 0.9,
            "task": {
                "title": "собрать демо",
                "owner_display_name": "Andre",
                "owner_user_id": "U-author",
            },
        },
        owner_payload={"reasoning": "no assignee mentioned", "display_name": None},
    )
    out = classify_with_backend(
        backend=backend,
        context=_ctx("надо собрать демо", author="U-author"),
        invocation_type=InvocationType.mention,
        source_text="надо собрать демо",
    )
    assert out.task.owner_user_id is None
    assert out.task.owner_display_name is None


def test_owner_call_only_runs_for_create_task():
    backend = _RecordingBackend(
        intent_payload={"intent": "no_action", "confidence": 0.1},
        owner_payload={"reasoning": "unused", "display_name": None},
    )
    classify_with_backend(
        backend=backend,
        context=_ctx("просто чатимся"),
        invocation_type=InvocationType.passive,
        source_text="просто чатимся",
    )
    kinds = [c["kind"] for c in backend.calls]
    assert OWNER_TOOL_NAME not in kinds


def test_owner_user_prompt_includes_conversation_context():
    prompt = build_owner_user_prompt(
        source_text="сделай это",
        context_messages=[
            {"ts": "0.5", "user": "U-alice", "text": "привет, Иван?"},
            {"ts": "1.0", "user": "U-bob", "text": "сделай это"},
        ],
        author_user_id="U-bob",
    )
    assert "source_author: U-bob" in prompt
    assert "NOT an assignee by default" in prompt
    assert "привет, Иван?" in prompt
    assert "source_message:" in prompt
    assert "сделай это" in prompt


def test_owner_call_failure_does_not_break_classification():
    class _BrokenOwner(_RecordingBackend):
        def call_tool(self, **kw):
            raise RuntimeError("openai down")

    backend = _BrokenOwner(
        intent_payload={
            "intent": "create_task",
            "confidence": 0.9,
            "task": {"title": "собрать демо"},
        },
        owner_payload=None,
    )
    out = classify_with_backend(
        backend=backend,
        context=_ctx("надо собрать демо"),
        invocation_type=InvocationType.mention,
        source_text="надо собрать демо",
    )
    # Main classification survived; owner stays null.
    assert out.task is not None
    assert out.task.title == "собрать демо"
    assert out.task.owner_user_id is None
