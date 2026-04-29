"""FR-CR-04-26 — Task.source_kind discriminator.

The 0014 migration adds a `source_kind` column with default 'slack'
so existing rows continue to round-trip; new tasks created via the
Telegram ingest path are 'telegram'.
"""
from __future__ import annotations

from app.models import Task, TaskPriority, TaskSourceKind, TaskStatus


def test_default_source_kind_is_slack(session):
    t = Task(title="x", priority=TaskPriority.medium, status=TaskStatus.todo)
    session.add(t)
    session.flush()
    assert t.source_kind == TaskSourceKind.slack


def test_explicit_telegram_source_kind_persists(session):
    t = Task(
        title="x",
        priority=TaskPriority.medium,
        status=TaskStatus.todo,
        source_kind=TaskSourceKind.telegram,
    )
    session.add(t)
    session.flush()

    session.expire_all()
    fetched = session.get(Task, t.id)
    assert fetched.source_kind == TaskSourceKind.telegram


def test_source_kind_enum_values():
    """FR-CR-05-39 — enum gained `fireflies` for tasks extracted
    from Fireflies meeting transcripts."""
    assert {k.value for k in TaskSourceKind} == {"slack", "telegram", "fireflies"}


def test_create_task_from_draft_routes_telegram_source_kind(session):
    """`source.kind` in the metadata dict is honoured by
    `create_task_from_draft`. Slack call sites pass nothing → default.
    Telegram ingest passes `kind: telegram` → flag is set."""
    from app.models import (
        ActionDraft,
        ActionDraftState,
        ContextSnapshot,
        IntentInference,
    )
    from app.models.intent import IntentType as IE
    from app.persistence import create_task_from_draft

    snap = ContextSnapshot(
        conversation_id="-100777",
        source_ts="42",
        source_message={"ts": "42", "text": "do x", "user": "99"},
        history_before=[],
        thread_messages=[],
    )
    session.add(snap)
    session.flush()
    inf = IntentInference(
        context_snapshot_id=snap.id,
        intent=IE.create_task,
        confidence=0.9,
        invocation_type="passive",
    )
    session.add(inf)
    session.flush()
    draft = ActionDraft(
        inference_id=inf.id,
        intent=IE.create_task,
        state=ActionDraftState.proposed,
        payload={"title": "do x"},
        slack_message_ts="42",
    )
    session.add(draft)
    session.flush()

    t = create_task_from_draft(
        session,
        draft=draft,
        source={
            "kind": "telegram",
            "conversation_id": "-100777",
            "message_ts": "42",
        },
        context_snapshot_id=snap.id,
        fallback_author_slack_id="99",
    )
    assert t.source_kind == TaskSourceKind.telegram
