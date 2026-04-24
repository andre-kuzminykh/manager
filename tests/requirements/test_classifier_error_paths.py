"""Requirement coverage: NFR-CR-04-1 (per-node resilience),
FR-CR-04-9 (prefilter safety net on pipeline failure).

classify_with_backend is the public entry for the intent classifier.
When the pipeline raises, it must degrade to the rule-based hint and
never propagate the exception up to the Slack handler."""
from __future__ import annotations

from unittest.mock import patch

from app.context.retriever import ContextWindow
from app.intent.classifier import classify_with_backend
from app.intent import IntentClassifier
from app.schemas.intent import InvocationType, IntentType


def _ctx(text="надо сделать X"):
    return ContextWindow(
        conversation_id="C1",
        source_ts="1.0",
        thread_ts=None,
        source_message={"ts": "1.0", "user": "U-author", "text": text},
    )


class _Backend:
    """Minimal stub that just records call_tool calls."""

    def extract_intent(self, *, user_prompt):  # pragma: no cover
        raise NotImplementedError

    def call_tool(self, **kw):
        return None


def test_classify_with_backend_falls_back_to_rules_on_pipeline_exception():
    backend = _Backend()
    with patch(
        "app.intent.classifier.run_pipeline",
        side_effect=RuntimeError("openai 500"),
    ):
        out = classify_with_backend(
            backend=backend,
            context=_ctx("надо собрать отчёт"),
            invocation_type=InvocationType.passive,
            source_text="надо собрать отчёт",
        )
    # Rules matched the "надо" keyword → create_task hint.
    assert out.intent == IntentType.create_task
    # Confidence is halved because we're degraded, per the contract.
    assert out.confidence > 0
    assert out.confidence < 0.5
    # Reasoning names the failure so operators can see it in logs.
    assert "pipeline error" in (out.reasoning or "").lower()


def test_classify_with_backend_returns_no_action_when_rules_also_silent():
    backend = _Backend()
    with patch(
        "app.intent.classifier.run_pipeline",
        side_effect=RuntimeError("openai 500"),
    ):
        out = classify_with_backend(
            backend=backend,
            context=_ctx("привет"),
            invocation_type=InvocationType.passive,
            source_text="привет",
        )
    assert out.intent == IntentType.no_action


def test_classifier_without_backend_falls_back_to_prefilter():
    classifier = IntentClassifier(backend=None)
    out = classifier.classify(
        context=_ctx("надо сделать задачу"),
        invocation_type=InvocationType.passive,
    )
    assert out.intent == IntentType.create_task
    assert "prefilter only" in (out.reasoning or "")


def test_classifier_without_backend_on_chat_returns_no_action():
    classifier = IntentClassifier(backend=None)
    out = classifier.classify(
        context=_ctx("привет"),
        invocation_type=InvocationType.passive,
    )
    assert out.intent == IntentType.no_action
