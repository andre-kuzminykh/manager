"""Requirement coverage: FR-CR-04-9 (prefilter is no longer a gate;
pipeline always runs on passive).

Passive messages used to short-circuit to no_action whenever the
rule-based prefilter found no task/meeting keyword. That fence was too
strict: real Russian phrasings like "нам нужно починить X к пятнице"
never reached the LLM detection stage. Now the pipeline runs on every
passive message and the prefilter only helps when no backend is
configured or as a safety net on top of the pipeline result."""
from __future__ import annotations

from app.context.retriever import ContextWindow
from app.intent import IntentClassifier
from app.intent.detect_prompt import DETECT_TOOL_NAME
from app.intent.owner_prompt import OWNER_TOOL_NAME
from app.intent.title_prompt import TITLE_TOOL_NAME
from app.schemas.intent import InvocationType, IntentType


class _RecordingBackend:
    """Records each tool call and returns canned payloads per stage."""

    def __init__(self, *, detect, title=None, owner=None):
        self._payloads = {
            DETECT_TOOL_NAME: detect,
            TITLE_TOOL_NAME: title or {"title": "stub title"},
            OWNER_TOOL_NAME: owner or {"reasoning": "no one", "display_name": None},
        }
        self.calls: list[str] = []

    def extract_intent(self, *, user_prompt):  # pragma: no cover
        raise NotImplementedError

    def call_tool(self, **kw):
        self.calls.append(kw["tool_name"])
        return self._payloads.get(kw["tool_name"])


def _ctx(text):
    return ContextWindow(
        conversation_id="C1",
        source_ts="1.0",
        thread_ts=None,
        source_message={"ts": "1.0", "user": "U-author", "text": text},
    )


def test_passive_runs_detection_even_without_prefilter_keyword():
    """«нам нужно починить вторую петлю красного реактора к следующей
    пятнице» — the prefilter used to see no match and short-circuit
    the whole pipeline. Now the detection LLM always gets a chance."""
    backend = _RecordingBackend(
        detect={"is_task": True, "confidence": 0.88, "reasoning": "imperative"},
        title={"title": "починить вторую петлю красного реактора"},
    )
    classifier = IntentClassifier(backend=backend)
    out = classifier.classify(
        context=_ctx("нам нужно починить вторую петлю красного реактора к следующей пятнице"),
        invocation_type=InvocationType.passive,
    )
    # The detection stage WAS called.
    assert DETECT_TOOL_NAME in backend.calls
    assert out.intent == IntentType.create_task
    assert out.task is not None


def test_passive_short_circuits_when_no_backend_configured():
    """No LLM → fall back to the rule hint. "нам нужно" used to miss the
    task keyword list — we added it, so the rules now catch it."""
    classifier = IntentClassifier(backend=None)
    out = classifier.classify(
        context=_ctx("нам нужно починить X"),
        invocation_type=InvocationType.passive,
    )
    assert out.intent == IntentType.create_task  # prefilter caught "нужно"
