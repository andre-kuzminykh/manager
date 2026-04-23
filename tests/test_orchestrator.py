from app.config import Settings
from app.orchestrator.service import ConfidenceBucket, Orchestrator, bucket_for
from app.schemas.intent import (
    IntentClassification,
    IntentType,
    InvocationType,
    TaskDraft,
)


def _settings() -> Settings:
    return Settings(
        INTENT_CONFIDENCE_HIGH=0.75,
        INTENT_CONFIDENCE_LOW=0.40,
    )


def test_bucket_thresholds():
    s = _settings()
    assert bucket_for(0.95, s) == ConfidenceBucket.high
    assert bucket_for(0.5, s) == ConfidenceBucket.medium
    assert bucket_for(0.1, s) == ConfidenceBucket.low


def test_no_action_is_always_silent():
    orch = Orchestrator(_settings())
    c = IntentClassification(intent=IntentType.no_action, confidence=0.99)
    d = orch.decide_passive(classification=c, draft_id=None)
    assert d.action == "silent"


def test_high_confidence_task_shows_card():
    orch = Orchestrator(_settings())
    c = IntentClassification(
        intent=IntentType.create_task,
        confidence=0.9,
        task=TaskDraft(title="Do X"),
    )
    d = orch.decide_passive(classification=c, draft_id=42)
    assert d.action == "card"
    assert d.confidence_bucket == ConfidenceBucket.high
    assert d.draft_id == 42


def test_medium_confidence_shows_soft_prompt():
    orch = Orchestrator(_settings())
    c = IntentClassification(
        intent=IntentType.create_task,
        confidence=0.5,
        task=TaskDraft(title="Maybe a task"),
    )
    d = orch.decide_passive(classification=c, draft_id=7)
    assert d.action == "soft_prompt"


def test_low_confidence_is_silent():
    orch = Orchestrator(_settings())
    c = IntentClassification(
        intent=IntentType.create_task,
        confidence=0.1,
        task=TaskDraft(title="Unclear"),
    )
    d = orch.decide_passive(classification=c, draft_id=7)
    assert d.action == "silent"
    assert d.draft_id is None


def test_invocation_type_passive_prefilter_short_circuits(monkeypatch):
    from app.intent.classifier import IntentClassifier
    from app.context.retriever import ContextWindow

    classifier = IntentClassifier(anthropic_client=None)
    ctx = ContextWindow(
        conversation_id="C1",
        source_ts="1.0",
        thread_ts=None,
        source_message={"ts": "1.0", "text": "hey how are you", "user": "U1"},
    )
    result = classifier.classify(context=ctx, invocation_type=InvocationType.passive)
    assert result.intent == IntentType.no_action
