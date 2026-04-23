from datetime import date, datetime, timezone

from app.models import (
    ActionDraft,
    ActionDraftState,
    ContextSnapshot,
    IntentInference,
)
from app.models.intent import IntentType as IntentTypeEnum
from app.persistence import (
    create_meeting_from_draft,
    create_task_from_draft,
    summarize_task,
)


def _make_draft(session, *, intent: IntentTypeEnum, payload: dict) -> ActionDraft:
    snap = ContextSnapshot(
        conversation_id="C1",
        source_ts="1.0",
        thread_ts=None,
        source_message={"ts": "1.0", "text": "x", "user": "U1"},
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
    return draft


def test_create_task_from_draft_persists_source_link(sqlite_session):
    draft = _make_draft(
        sqlite_session,
        intent=IntentTypeEnum.create_task,
        payload={
            "title": "Do it",
            "description": "details",
            "owner_display_name": "Ivan",
            "priority": "high",
            "due_date": "2026-05-01",
        },
    )

    task = create_task_from_draft(
        sqlite_session,
        draft=draft,
        source={
            "conversation_id": "C1",
            "message_ts": "1.0",
            "thread_ts": None,
            "permalink": "https://slack.com/archives/C1/p1",
        },
        context_snapshot_id=1,
        fallback_author_slack_id="U1",
    )

    assert task.id is not None
    assert task.due_date == date(2026, 5, 1)
    assert task.source_conversation_id == "C1"
    assert task.source_message_ts == "1.0"
    assert task.source_permalink.endswith("p1")
    assert draft.state == ActionDraftState.confirmed
    assert "Do it" in summarize_task(task)


def test_create_task_rejects_empty_title(sqlite_session):
    draft = _make_draft(
        sqlite_session,
        intent=IntentTypeEnum.create_task,
        payload={"title": ""},
    )
    import pytest

    with pytest.raises(ValueError):
        create_task_from_draft(
            sqlite_session,
            draft=draft,
            source={},
            context_snapshot_id=None,
            fallback_author_slack_id=None,
        )


def test_create_meeting_parses_datetime_and_participants(sqlite_session):
    draft = _make_draft(
        sqlite_session,
        intent=IntentTypeEnum.create_meeting,
        payload={
            "title": "Product sync",
            "participants": "Ivan, @anna",
            "datetime_at": "2026-05-02T15:00:00+00:00",
            "notes": "quick chat",
        },
    )
    meeting = create_meeting_from_draft(
        sqlite_session,
        draft=draft,
        source={
            "conversation_id": "C1",
            "message_ts": "1.0",
            "thread_ts": None,
            "permalink": None,
        },
        context_snapshot_id=1,
        fallback_author_slack_id="U1",
    )
    assert meeting.title == "Product sync"
    assert meeting.participants == ["Ivan", "@anna"]
    assert meeting.datetime_at == datetime(2026, 5, 2, 15, 0, tzinfo=timezone.utc)
    assert draft.state == ActionDraftState.confirmed
