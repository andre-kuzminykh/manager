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
    from Fireflies meeting transcripts.
    FR-CR-05-116 — enum gained `zoom` for tasks extracted from
    Zoom Cloud Recording transcripts."""
    assert {k.value for k in TaskSourceKind} == {
        "slack", "telegram", "fireflies", "zoom"
    }


def test_postgres_enum_extension_pinned_in_migrations():
    """FR-CR-05-118 regression guard. Postgres uses an actual
    enum type for `task_source_kind`, so adding a new value to
    the Python enum is NOT enough — there must be an alembic
    migration that runs `ALTER TYPE task_source_kind ADD VALUE`
    for it. SQLite tests don't catch this (the column is a
    string under the hood), so this test scans the migration
    files and asserts every `TaskSourceKind` value is mentioned
    somewhere in alembic.

    The bug we're guarding against: 0020_zoom_recordings created
    the `zoom_recordings` table but forgot the enum extension;
    production Postgres rejected `INSERT … source_kind='zoom'`
    with «invalid input value for enum task_source_kind:
    \"zoom\"» and rolled back the whole pipeline transaction.
    0021_task_source_kind_zoom fixed it, but the same gap could
    re-open if another value is added later without the matching
    migration."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    versions = root / "alembic" / "versions"
    blob = ""
    for f in versions.glob("*.py"):
        blob += f.read_text(encoding="utf-8")

    for value in (k.value for k in TaskSourceKind):
        # Either an explicit `ALTER TYPE … ADD VALUE 'X'` for
        # the new value (FR-CR-05-39 fireflies, FR-CR-05-118
        # zoom) or the original `Enum(...)`-inline list at
        # initial-migration time (slack, telegram).
        assert (
            f"ADD VALUE IF NOT EXISTS '{value}'" in blob
            or f"ADD VALUE '{value}'" in blob
            or f"'{value}'" in blob
        ), (
            f"TaskSourceKind value {value!r} not found in any "
            f"alembic migration. Add a migration that runs "
            f"`ALTER TYPE task_source_kind ADD VALUE "
            f"IF NOT EXISTS '{value}'` (mirror of 0021)."
        )


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
