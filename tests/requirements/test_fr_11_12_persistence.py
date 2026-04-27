"""Tests for FR-11..FR-12 (DB is the source of truth + source linkage)."""
from __future__ import annotations

from datetime import date

import pytest

from app.models import (
    ActionDraft,
    ActionDraftState,
    AuditLog,
    ContextSnapshot,
    IntentInference,
    Task,
)
from app.models.intent import IntentType as IE
from app.models.task import TaskPriority, TaskStatus
from app.persistence import create_task_from_draft


def _prep(session, intent=IE.create_task, payload=None):
    payload = payload or {"title": "t"}
    snap = ContextSnapshot(
        conversation_id="C1",
        source_ts="1.0",
        source_message={"ts": "1.0", "text": "src", "user": "U1"},
        history_before=[],
        thread_messages=[],
    )
    session.add(snap)
    session.flush()
    inf = IntentInference(
        context_snapshot_id=snap.id,
        intent=intent,
        confidence=0.9,
        invocation_type="mention",
    )
    session.add(inf)
    session.flush()
    draft = ActionDraft(
        inference_id=inf.id,
        intent=intent,
        state=ActionDraftState.proposed,
        payload=payload,
        created_by_slack_user_id="U1",
        slack_message_ts="1.0",
    )
    session.add(draft)
    session.flush()
    return draft, snap


# =============================================================================
# FR-11: Source of truth = DB.
# =============================================================================


def test_fr11_task_row_exists_after_create(session):
    draft, snap = _prep(session)
    t = create_task_from_draft(
        session,
        draft=draft,
        source={"conversation_id": "C1", "message_ts": "1.0", "thread_ts": None, "permalink": "p"},
        context_snapshot_id=snap.id,
        fallback_author_slack_id="U1",
    )
    assert t.id is not None
    # Reload by id to prove durability
    assert session.get(Task, t.id) is not None


def test_fr11_task_defaults_are_applied(session):
    draft, snap = _prep(session, payload={"title": "t"})
    t = create_task_from_draft(
        session,
        draft=draft,
        source={},
        context_snapshot_id=snap.id,
        fallback_author_slack_id="U1",
    )
    assert t.priority == TaskPriority.medium
    # CR-01: new tasks without a due date land in Backlog
    assert t.status == TaskStatus.backlog


@pytest.mark.parametrize("priority", ["low", "medium", "high", "urgent"])
def test_fr11_priority_is_persisted(session, priority):
    draft, snap = _prep(session, payload={"title": "t", "priority": priority})
    t = create_task_from_draft(
        session,
        draft=draft,
        source={},
        context_snapshot_id=snap.id,
        fallback_author_slack_id="U1",
    )
    assert t.priority.value == priority


def test_fr11_due_date_parsed_from_iso(session):
    draft, snap = _prep(session, payload={"title": "t", "due_date": "2026-05-15"})
    t = create_task_from_draft(
        session, draft=draft, source={}, context_snapshot_id=snap.id, fallback_author_slack_id="U1"
    )
    assert t.due_date == date(2026, 5, 15)


def test_fr11_invalid_due_date_becomes_none(session):
    draft, snap = _prep(session, payload={"title": "t", "due_date": "not-a-date"})
    t = create_task_from_draft(
        session, draft=draft, source={}, context_snapshot_id=snap.id, fallback_author_slack_id="U1"
    )
    assert t.due_date is None


def test_fr11_empty_title_rejected(session):
    draft, snap = _prep(session, payload={"title": ""})
    with pytest.raises(ValueError):
        create_task_from_draft(
            session,
            draft=draft,
            source={},
            context_snapshot_id=snap.id,
            fallback_author_slack_id="U1",
        )


def test_fr11_whitespace_only_title_rejected(session):
    draft, snap = _prep(session, payload={"title": "   "})
    with pytest.raises(ValueError):
        create_task_from_draft(
            session,
            draft=draft,
            source={},
            context_snapshot_id=snap.id,
            fallback_author_slack_id="U1",
        )


def test_fr11_finalize_persists_entity_even_if_sync_disabled(
    patched_session_scope, SessionFactory
):
    from app.config import Settings
    from app.orchestrator.finalize import FinalizeService

    with SessionFactory() as s:
        draft, snap = _prep(s)
        s.commit()
        draft_id = draft.id
        snap_id = snap.id

    fin = FinalizeService(settings=Settings())  # no sync factories
    entity_type, entity_id, _ = fin.finalize_draft(
        draft_id=draft_id,
        source_metadata={
            "conversation_id": "C1",
            "message_ts": "1.0",
            "thread_ts": None,
            "permalink": "p",
            "context_snapshot_id": snap_id,
            "source_user_id": "U1",
        },
    )
    assert entity_type == "task"
    assert entity_id is not None

    with SessionFactory() as s:
        assert s.query(Task).count() == 1


def test_fr11_finalize_marks_draft_as_confirmed(patched_session_scope, SessionFactory):
    from app.config import Settings
    from app.orchestrator.finalize import FinalizeService

    with SessionFactory() as s:
        draft, snap = _prep(s)
        s.commit()
        draft_id, snap_id = draft.id, snap.id

    FinalizeService(settings=Settings()).finalize_draft(
        draft_id=draft_id,
        source_metadata={
            "conversation_id": "C1",
            "message_ts": "1.0",
            "thread_ts": None,
            "permalink": "p",
            "context_snapshot_id": snap_id,
        },
    )
    with SessionFactory() as s:
        assert s.get(ActionDraft, draft_id).state == ActionDraftState.confirmed


def test_fr11_finalize_emits_audit_log(patched_session_scope, SessionFactory):
    from app.config import Settings
    from app.orchestrator.finalize import FinalizeService

    with SessionFactory() as s:
        draft, snap = _prep(s)
        s.commit()
        draft_id, snap_id = draft.id, snap.id

    FinalizeService(settings=Settings()).finalize_draft(
        draft_id=draft_id,
        source_metadata={
            "conversation_id": "C1",
            "message_ts": "1.0",
            "thread_ts": None,
            "permalink": "p",
            "context_snapshot_id": snap_id,
        },
    )
    with SessionFactory() as s:
        logs = s.query(AuditLog).all()
        assert any(l.action == "task_created" for l in logs)


def test_fr11_finalize_rejects_unknown_draft(patched_session_scope):
    from app.config import Settings
    from app.orchestrator.finalize import FinalizeService

    with pytest.raises(ValueError):
        FinalizeService(settings=Settings()).finalize_draft(
            draft_id=999999, source_metadata={}
        )


def test_fr11_finalize_rejects_already_confirmed(patched_session_scope, SessionFactory):
    from app.config import Settings
    from app.orchestrator.finalize import FinalizeService

    with SessionFactory() as s:
        draft, snap = _prep(s)
        draft.state = ActionDraftState.confirmed
        s.commit()
        draft_id = draft.id

    with pytest.raises(ValueError):
        FinalizeService(settings=Settings()).finalize_draft(
            draft_id=draft_id, source_metadata={}
        )


# =============================================================================
# FR-12: Every entity keeps source-message traceability.
# =============================================================================


def test_fr12_task_keeps_source_conversation_id(session):
    draft, snap = _prep(session)
    t = create_task_from_draft(
        session,
        draft=draft,
        source={"conversation_id": "C1", "message_ts": "1.0", "thread_ts": None, "permalink": "p"},
        context_snapshot_id=snap.id,
        fallback_author_slack_id="U1",
    )
    assert t.source_conversation_id == "C1"


def test_fr12_task_keeps_source_message_ts(session):
    draft, snap = _prep(session)
    t = create_task_from_draft(
        session,
        draft=draft,
        source={"conversation_id": "C1", "message_ts": "7.42", "thread_ts": None, "permalink": "p"},
        context_snapshot_id=snap.id,
        fallback_author_slack_id="U1",
    )
    assert t.source_message_ts == "7.42"


def test_fr12_task_keeps_thread_ts_when_in_thread(session):
    draft, snap = _prep(session)
    t = create_task_from_draft(
        session,
        draft=draft,
        source={"conversation_id": "C1", "message_ts": "2.0", "thread_ts": "1.0", "permalink": "p"},
        context_snapshot_id=snap.id,
        fallback_author_slack_id="U1",
    )
    assert t.source_thread_ts == "1.0"


def test_fr12_task_keeps_permalink(session):
    draft, snap = _prep(session)
    url = "https://workspace.slack.com/archives/C1/p1000"
    t = create_task_from_draft(
        session,
        draft=draft,
        source={"conversation_id": "C1", "message_ts": "1.0", "thread_ts": None, "permalink": url},
        context_snapshot_id=snap.id,
        fallback_author_slack_id="U1",
    )
    assert t.source_permalink == url


def test_fr12_task_keeps_context_snapshot_reference(session):
    draft, snap = _prep(session)
    t = create_task_from_draft(
        session,
        draft=draft,
        source={},
        context_snapshot_id=snap.id,
        fallback_author_slack_id="U1",
    )
    assert t.context_snapshot_id == snap.id


def test_fr12_owner_fallback_to_source_author(session):
    draft, _ = _prep(session)
    draft.created_by_slack_user_id = None  # no explicit owner
    t = create_task_from_draft(
        session,
        draft=draft,
        source={},
        context_snapshot_id=None,
        fallback_author_slack_id="U42",
    )
    assert t.owner_user_id == "U42"


def test_fr12_explicit_owner_in_payload_wins_over_fallback(session):
    draft, _ = _prep(
        session,
        payload={"title": "t", "owner_user_id": "U-explicit"},
    )
    t = create_task_from_draft(
        session,
        draft=draft,
        source={},
        context_snapshot_id=None,
        fallback_author_slack_id="U-fallback",
    )
    assert t.owner_user_id == "U-explicit"


def test_fr12_context_snapshot_stores_history_and_thread(session):
    snap = ContextSnapshot(
        conversation_id="C1",
        source_ts="2.0",
        thread_ts="1.0",
        source_message={"ts": "2.0", "text": "src", "user": "U1"},
        history_before=[{"ts": "1.0", "text": "prev", "user": "U2"}],
        thread_messages=[{"ts": "1.0", "text": "root", "user": "U1"}],
    )
    session.add(snap)
    session.flush()
    assert session.get(ContextSnapshot, snap.id).history_before[0]["text"] == "prev"
    assert session.get(ContextSnapshot, snap.id).thread_messages[0]["text"] == "root"


def test_fr12_task_source_metadata_is_preserved_via_finalize(
    patched_session_scope, SessionFactory
):
    from app.config import Settings
    from app.orchestrator.finalize import FinalizeService

    with SessionFactory() as s:
        draft, snap = _prep(s)
        s.commit()
        draft_id, snap_id = draft.id, snap.id

    FinalizeService(settings=Settings()).finalize_draft(
        draft_id=draft_id,
        source_metadata={
            "conversation_id": "CZ",
            "message_ts": "55.5",
            "thread_ts": "54.0",
            "permalink": "https://p/z",
            "context_snapshot_id": snap_id,
            "source_user_id": "U99",
        },
    )
    with SessionFactory() as s:
        t = s.query(Task).one()
        assert t.source_conversation_id == "CZ"
        assert t.source_message_ts == "55.5"
        assert t.source_thread_ts == "54.0"
        assert t.source_permalink == "https://p/z"
        assert t.context_snapshot_id == snap_id
        assert t.created_by_slack_user_id == "U1"  # from draft
