"""Tests for NFR-1..NFR-5:

- NFR-1: Event ingestion acks fast, heavy work async.
- NFR-2: Duplicate Slack events are not re-processed.
- NFR-3: Raw + normalized payload persisted for audit.
- NFR-4: Passive mode never auto-creates entities.
- NFR-5: Behavior in ambiguous cases is predictable.
"""
from __future__ import annotations

import pytest

from app.models import (
    ActionDraft,
    ActionDraftState,
    ContextSnapshot,
    IntentInference,
    Meeting,
    ProcessedSlackEvent,
    SlackMessage,
    Task,
)
from app.schemas.intent import IntentClassification, IntentType, TaskDraft
from app.orchestrator import Orchestrator
from app.config import Settings


# =============================================================================
# NFR-1: Event ingestion is async — ack() called before heavy work.
# =============================================================================


class _OrderedAck:
    """Records the ordinal at which ack() is called vs. session_scope()."""

    def __init__(self) -> None:
        self.order: list[str] = []

    def ack(self, *a, **kw) -> None:
        self.order.append("ack")


def _patch_session_scope_order(monkeypatch, order: list[str]):
    import contextlib

    @contextlib.contextmanager
    def tracked():
        order.append("session")
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.models import Base

        eng = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(eng)
        s = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()
            eng.dispose()

    for t in [
        "app.slack_bot.handlers.events.session_scope",
        "app.slack_bot.handlers.shortcuts.session_scope",
        "app.slack_bot.handlers.actions.session_scope",
        "app.slack_bot.handlers.views.session_scope",
    ]:
        monkeypatch.setattr(t, tracked, raising=False)


def test_nfr1_handle_message_acks_before_db_work(
    monkeypatch, services, sender, bolt_context, slack_client
):
    from app.slack_bot.handlers.events import handle_message

    order: list[str] = []
    _patch_session_scope_order(monkeypatch, order)

    class AckOrder:
        def __call__(self, *a, **kw):
            order.append("ack")

    handle_message(
        event={
            "ts": "1.0",
            "user": "U1",
            "text": "надо задачу",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "EvAck-1"},
        client=slack_client,
        context=bolt_context,
        services=services,
        sender=sender,
        ack=AckOrder(),
    )
    assert order[0] == "ack"


def test_nfr1_handle_app_mention_acks_before_db_work(
    monkeypatch, services_task, sender, bolt_context, slack_client
):
    from app.slack_bot.handlers.events import handle_app_mention

    order: list[str] = []
    _patch_session_scope_order(monkeypatch, order)

    class AckOrder:
        def __call__(self, *a, **kw):
            order.append("ack")

    handle_app_mention(
        event={
            "ts": "2.0",
            "user": "U1",
            "text": "<@UBOT> create",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "EvAck-2"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=sender,
        ack=AckOrder(),
    )
    assert order[0] == "ack"


def test_nfr1_shortcut_acks_before_views_open(
    monkeypatch, services_task, slack_client
):
    from app.slack_bot.handlers.shortcuts import (
        SHORTCUT_CREATE_TASK,
        handle_shortcut,
    )

    order: list[str] = []
    _patch_session_scope_order(monkeypatch, order)

    opened_after: list[bool] = []

    class AckOrder:
        def __call__(self, *a, **kw):
            order.append("ack")
            opened_after.append(bool(slack_client.views_opened))

    handle_shortcut(
        shortcut={
            "callback_id": SHORTCUT_CREATE_TASK,
            "trigger_id": "t",
            "channel": {"id": "C1"},
            "user": {"id": "U1"},
            "message": {"ts": "1.0", "user": "U1", "text": "x"},
        },
        client=slack_client,
        services=services_task,
        ack=AckOrder(),
    )
    # views_open must not have been called before ack.
    assert opened_after[0] is False


def test_nfr1_confirm_action_acks_before_finalize(
    patched_session_scope, SessionFactory, finalizer_stub, slack_client, sender
):
    from app.slack_bot.handlers.actions import handle_confirm
    from app.models.intent import IntentType as IE
    from tests.test_persistence import _make_draft

    with SessionFactory() as s:
        d = _make_draft(s, intent=IE.create_task, payload={"title": "t"})
        s.commit()
        did = d.id

    order: list[str] = []

    class TrackingFinalize:
        def finalize_draft(self, *, draft_id, source_metadata):
            order.append("finalize")
            return ("task", 1, "sum")

    class AckOrder:
        def __call__(self, *a, **kw):
            order.append("ack")

    handle_confirm(
        body={
            "actions": [{"value": str(did)}],
            "channel": {"id": "C1"},
            "message": {"metadata": {}},
        },
        client=slack_client,
        services=None,
        finalizer=TrackingFinalize(),
        sender=sender,
        ack=AckOrder(),
    )
    assert order.index("ack") < order.index("finalize")


# =============================================================================
# NFR-2: Duplicate events not reprocessed as new entity.
# =============================================================================


def test_nfr2_same_event_id_not_double_processed(
    patched_session_scope, services_task, sender, ack, bolt_context, slack_client, SessionFactory
):
    from app.slack_bot.handlers.events import handle_app_mention

    event = {
        "ts": "5.0",
        "user": "U1",
        "text": "<@UBOT> создай задачу",
        "channel": "C1",
        "channel_type": "channel",
    }
    body = {"event_id": "DedupEv-1"}
    for _ in range(3):
        handle_app_mention(
            event=event,
            body=body,
            client=slack_client,
            context=bolt_context,
            services=services_task,
            sender=sender,
            ack=ack,
        )
    with SessionFactory() as s:
        assert s.query(IntentInference).count() == 1
        assert s.query(ActionDraft).count() == 1


def test_nfr2_dedup_persists_event_id(patched_session_scope, SessionFactory):
    from app.slack_bot.dedup import claim_event

    with SessionFactory() as s:
        assert claim_event(s, "Ev-xyz") is True
        s.commit()

    with SessionFactory() as s:
        assert claim_event(s, "Ev-xyz") is False


def test_nfr2_dedup_different_ids_independent(patched_session_scope, SessionFactory):
    from app.slack_bot.dedup import claim_event

    with SessionFactory() as s:
        assert claim_event(s, "Ev-1") is True
        assert claim_event(s, "Ev-2") is True
        s.commit()
    with SessionFactory() as s:
        assert s.query(ProcessedSlackEvent).count() == 2


def test_nfr2_dedup_retry_from_slack_does_not_post_new_card(
    patched_session_scope, services_task, sender, ack, bolt_context, slack_client, SessionFactory
):
    from app.slack_bot.handlers.events import handle_message

    event = {
        "ts": "6.0",
        "user": "U1",
        "text": "сделай задачу",
        "channel": "C1",
        "channel_type": "channel",
    }
    for _ in range(3):
        handle_message(
            event=event,
            body={"event_id": "DedupEv-2"},
            client=slack_client,
            context=bolt_context,
            services=services_task,
            sender=sender,
            ack=ack,
        )
    # CR-03 always-create for passive: first handle posts the channel
    # task-card + an owner DM mirror (2 posts). Retries with the same
    # event_id must be silently deduped — so the total count is still 2.
    first_run_count = len(sender.posted)
    assert first_run_count == 2


def test_nfr2_missing_event_id_still_processed_once_per_call(
    patched_session_scope, services, sender, ack, bolt_context, slack_client, SessionFactory
):
    from app.slack_bot.handlers.events import handle_message

    handle_message(
        event={
            "ts": "7.0",
            "user": "U1",
            "text": "сделай задачу",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={},  # no event_id
        client=slack_client,
        context=bolt_context,
        services=services,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        # Missing id → treated as fresh, inference is created.
        assert s.query(IntentInference).count() == 1


def test_nfr2_deduplication_is_atomic_on_postgres_dialect_branch(SessionFactory):
    """Unit check of claim_event on sqlite fallback path."""
    from app.slack_bot.dedup import claim_event

    with SessionFactory() as s:
        assert claim_event(s, "race-1")
        # Simulate a second attempt inside the same uncommitted session:
        assert claim_event(s, "race-1") is False


# =============================================================================
# NFR-3: Raw + normalized payload persisted for audit.
# =============================================================================


def test_nfr3_context_snapshot_stores_source_history_thread(
    patched_session_scope, services_task, sender, ack, bolt_context, slack_client, SessionFactory
):
    from app.slack_bot.handlers.events import handle_message

    slack_client.history_messages = [
        {"ts": "0.5", "user": "U2", "text": "prev"},
    ]
    slack_client.replies_messages = [
        {"ts": "0.9", "user": "U2", "text": "root"},
        {"ts": "1.0", "user": "U1", "text": "reply"},
    ]
    handle_message(
        event={
            "ts": "1.0",
            "thread_ts": "0.9",
            "user": "U1",
            "text": "сделай задачу",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Snap-1"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        snap = s.query(ContextSnapshot).one()
        assert snap.source_message["text"] == "сделай задачу"
        assert snap.history_before and snap.history_before[0]["text"] == "prev"
        assert any(m["text"] == "root" for m in snap.thread_messages)


def test_nfr3_slack_message_row_stored_on_first_sighting(
    patched_session_scope, services_task, sender, ack, bolt_context, slack_client, SessionFactory
):
    from app.slack_bot.handlers.events import handle_message

    handle_message(
        event={
            "ts": "8.0",
            "user": "U1",
            "text": "task",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Raw-1"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        msg = s.query(SlackMessage).filter_by(ts="8.0").one()
        assert msg.text == "task"


def test_nfr3_intent_inference_raw_contains_task_dump(
    patched_session_scope, services_task, sender, ack, bolt_context, slack_client, SessionFactory
):
    from app.slack_bot.handlers.events import handle_app_mention

    handle_app_mention(
        event={
            "ts": "9.0",
            "user": "U1",
            "text": "<@UBOT> создай",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Raw-2"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        inf = s.query(IntentInference).one()
        assert inf.raw["task"]["title"]
        assert inf.raw["meeting"] is None


def test_nfr3_intent_inference_raw_contains_meeting_dump(
    patched_session_scope,
    services_meeting,
    sender,
    ack,
    bolt_context,
    slack_client,
    SessionFactory,
):
    from app.slack_bot.handlers.events import handle_app_mention

    handle_app_mention(
        event={
            "ts": "10.0",
            "user": "U1",
            "text": "<@UBOT> встреча",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Raw-3"},
        client=slack_client,
        context=bolt_context,
        services=services_meeting,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        inf = s.query(IntentInference).one()
        assert inf.raw["meeting"]["title"]


def test_nfr3_draft_payload_is_persisted_structured(
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
            "text": "<@UBOT> x",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Raw-4"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        d = s.query(ActionDraft).one()
        assert isinstance(d.payload, dict)
        assert "title" in d.payload


# =============================================================================
# NFR-4: Passive mode never auto-creates entities.
# =============================================================================


def test_nfr4_passive_high_confidence_auto_creates_task_per_cr03(
    patched_session_scope,
    services_task,
    sender,
    ack,
    bolt_context,
    slack_client,
    SessionFactory,
):
    """CR-03 FR-CR-03-3 superseded the original NFR-4 'never auto-create'
    rule: on high confidence the bot creates the task immediately so the
    admin can review/reject it rather than losing the signal."""
    from app.slack_bot.handlers.events import handle_message

    handle_message(
        event={
            "ts": "12.0",
            "user": "U1",
            "text": "надо сделать задачу",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Passive-1"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        assert s.query(ActionDraft).count() == 1
        assert s.query(Task).count() == 1  # CR-03: auto-created
        assert s.query(Meeting).count() == 0


def test_nfr4_passive_medium_confidence_produces_no_task(
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
            "ts": "13.0",
            "user": "U1",
            "text": "возможно задачу",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Passive-2"},
        client=slack_client,
        context=bolt_context,
        services=services_medium,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        assert s.query(Task).count() == 0


def test_nfr4_passive_low_confidence_is_silent(
    patched_session_scope, services_silent, sender, ack, bolt_context, slack_client
):
    from app.slack_bot.handlers.events import handle_message

    handle_message(
        event={
            "ts": "14.0",
            "user": "U1",
            "text": "привет",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Passive-3"},
        client=slack_client,
        context=bolt_context,
        services=services_silent,
        sender=sender,
        ack=ack,
    )
    assert sender.posted == []


def test_nfr4_soft_prompt_draft_still_awaits_user_confirmation(
    patched_session_scope, services_medium, sender, ack, bolt_context, slack_client, SessionFactory
):
    """Medium confidence still falls into the old draft flow — the user
    decides via soft prompt, and the draft stays 'proposed' until they do."""
    from app.slack_bot.handlers.events import handle_message

    handle_message(
        event={
            "ts": "15.0",
            "user": "U1",
            "text": "надо задачу",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Passive-4"},
        client=slack_client,
        context=bolt_context,
        services=services_medium,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        assert s.query(ActionDraft).one().state == ActionDraftState.proposed


# =============================================================================
# NFR-5: Predictable behavior in ambiguous cases.
# =============================================================================


def _orch() -> Orchestrator:
    return Orchestrator(Settings(INTENT_CONFIDENCE_HIGH=0.75, INTENT_CONFIDENCE_LOW=0.4))


@pytest.mark.parametrize("conf", [0.0, 0.3, 0.5, 0.7, 0.9])
def test_nfr5_decide_passive_is_deterministic(conf):
    """Same input twice → identical decision."""
    c = IntentClassification(
        intent=IntentType.create_task, confidence=conf, task=TaskDraft(title="t")
    )
    d1 = _orch().decide_passive(classification=c, draft_id=1)
    d2 = _orch().decide_passive(classification=c, draft_id=1)
    assert (d1.action, d1.confidence_bucket) == (d2.action, d2.confidence_bucket)


def test_nfr5_no_action_is_always_silent_regardless_of_confidence():
    for conf in (0.0, 0.5, 0.99):
        c = IntentClassification(intent=IntentType.no_action, confidence=conf)
        assert _orch().decide_passive(classification=c, draft_id=None).action == "silent"


def test_nfr5_low_confidence_forces_silent():
    c = IntentClassification(
        intent=IntentType.create_task, confidence=0.1, task=TaskDraft(title="t")
    )
    assert _orch().decide_passive(classification=c, draft_id=5).action == "silent"


def test_nfr5_silent_decision_clears_draft_id():
    c = IntentClassification(
        intent=IntentType.create_task, confidence=0.1, task=TaskDraft(title="t")
    )
    d = _orch().decide_passive(classification=c, draft_id=123)
    assert d.draft_id is None


def test_nfr5_soft_prompt_shows_only_yes_no_buttons():
    from app.slack_bot import blocks as bk

    payload = bk.soft_prompt(IntentType.create_task, draft_id=1)
    actions = next(b for b in payload if b["type"] == "actions")
    ids = [a["action_id"] for a in actions["elements"]]
    assert ids == [bk.ACTION_CONFIRM, bk.ACTION_IGNORE]


def test_nfr5_explicit_passive_prefilter_silences_chat():
    from app.context.retriever import ContextWindow
    from app.intent import IntentClassifier
    from app.schemas.intent import InvocationType

    classifier = IntentClassifier(anthropic_client=None)
    ctx = ContextWindow(
        conversation_id="C1",
        source_ts="1.0",
        thread_ts=None,
        source_message={"ts": "1.0", "text": "hi how are you", "user": "U1"},
    )
    result = classifier.classify(context=ctx, invocation_type=InvocationType.passive)
    assert result.intent == IntentType.no_action
