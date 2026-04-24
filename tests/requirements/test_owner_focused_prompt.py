"""Requirement coverage: FR-CR-04-4 (focused owner prompt with
conversation context), NFR-CR-04-1 (owner-stage failure isolation).

Owner detection runs as a SEPARATE LLM call inside the pipeline,
using a dedicated system prompt that knows nothing about intent or
dates. This keeps owner accuracy high on small models.
"""
from __future__ import annotations

from app.context.retriever import ContextWindow
from app.intent.classifier import classify_with_backend
from app.intent.owner_prompt import OWNER_SYSTEM_PROMPT, OWNER_TOOL_NAME
from app.intent.detect_prompt import DETECT_TOOL_NAME
from app.intent.title_prompt import TITLE_TOOL_NAME
from app.schemas.intent import InvocationType


class _PipelineBackend:
    """Records every stage call and dispatches a canned response per tool."""

    def __init__(self, *, detect, title, owner):
        self._payloads = {
            DETECT_TOOL_NAME: detect,
            TITLE_TOOL_NAME: title,
            OWNER_TOOL_NAME: owner,
        }
        self.calls: list[dict] = []

    def extract_intent(self, *, user_prompt):  # pragma: no cover — unused
        raise NotImplementedError

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
                "tool_name": tool_name,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
            }
        )
        return self._payloads.get(tool_name)


def _ctx(source_text, author="U-author", history=()):
    return ContextWindow(
        conversation_id="C1",
        source_ts="1.0",
        thread_ts=None,
        source_message={"ts": "1.0", "user": author, "text": source_text},
        history_before=list(history),
    )


def test_owner_call_uses_dedicated_system_prompt():
    backend = _PipelineBackend(
        detect={"is_task": True, "confidence": 0.9},
        title={"title": "собрать демо"},
        owner={"reasoning": "no assignee", "display_name": None},
    )
    classify_with_backend(
        backend=backend,
        context=_ctx("надо собрать демо"),
        invocation_type=InvocationType.mention,
        source_text="надо собрать демо",
    )
    owner_call = next(c for c in backend.calls if c["tool_name"] == OWNER_TOOL_NAME)
    # Owner system prompt is different from detect / title — it knows only
    # about assignees, nothing about tasks or dates.
    assert owner_call["system_prompt"] == OWNER_SYSTEM_PROMPT
    assert "ASSIGNEE" in owner_call["system_prompt"]
    assert "intent" not in owner_call["system_prompt"].lower()


def test_owner_call_fills_slack_user_id_when_returned():
    backend = _PipelineBackend(
        detect={"is_task": True, "confidence": 0.9},
        title={"title": "сделай демо"},
        owner={
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


def test_owner_call_only_runs_when_task_detected():
    """Detection says "not a task" → pipeline short-circuits, no owner call."""
    backend = _PipelineBackend(
        detect={"is_task": False, "confidence": 0.1},
        title={"title": "unused"},
        owner={"reasoning": "unused", "display_name": None},
    )
    classify_with_backend(
        backend=backend,
        context=_ctx("просто чатимся"),
        invocation_type=InvocationType.passive,
        source_text="просто чатимся",
    )
    kinds = [c["tool_name"] for c in backend.calls]
    assert DETECT_TOOL_NAME in kinds
    assert OWNER_TOOL_NAME not in kinds
    assert TITLE_TOOL_NAME not in kinds


def test_owner_user_prompt_includes_conversation_context():
    from app.intent.owner_prompt import build_owner_user_prompt

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
    """If the owner stage raises, the whole classification still succeeds;
    we just leave owner fields null."""

    class _BrokenOwner(_PipelineBackend):
        def call_tool(self, **kw):
            if kw.get("tool_name") == OWNER_TOOL_NAME:
                raise RuntimeError("openai down")
            return super().call_tool(**kw)

    backend = _BrokenOwner(
        detect={"is_task": True, "confidence": 0.9},
        title={"title": "собрать демо"},
        owner={"reasoning": "unused", "display_name": None},
    )
    out = classify_with_backend(
        backend=backend,
        context=_ctx("надо собрать демо"),
        invocation_type=InvocationType.mention,
        source_text="надо собрать демо",
    )
    assert out.task is not None
    assert out.task.title == "собрать демо"
    assert out.task.owner_user_id is None
