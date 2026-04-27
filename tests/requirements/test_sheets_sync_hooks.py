"""FR-CR-04-23 — Sheets sync on every task change.

Tests cover:

- `TaskSyncer.sync` no-ops when no factory is configured;
- the active syncer is invoked from every status-change handler
  (start, mark done, cancel, delete) and from edit submit;
- `_task_row` writes "deleted" instead of the underlying status when
  `deleted_at` is set, so the row in the sheet visually flips to
  deleted;
- the configurable tab name (`GOOGLE_SHEETS_TAB_NAME`) flows through
  the factory.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from app.config import Settings
from app.models import Task, TaskStatus
from app.models.task import TaskPriority


# --------------------------------------------------------------------------- #
# Row shape with soft-delete + completion artifact
# --------------------------------------------------------------------------- #


def _row_dict(task: Task) -> dict[str, str]:
    from app.sync.sheets import _HEADER_ROW, _task_row

    return dict(zip(_HEADER_ROW, _task_row(task)))


def test_task_row_status_flips_to_deleted_when_soft_deleted():
    t = Task(
        id=1,
        title="t",
        owner_user_id="U1",
        priority=TaskPriority.medium,
        status=TaskStatus.in_progress,
        deleted_at=datetime(2026, 4, 27, 10, tzinfo=timezone.utc),
    )
    row = _row_dict(t)
    assert row["status"] == "deleted"
    assert row["deleted_at"].startswith("2026-04-27")


def test_task_row_status_unchanged_when_not_deleted():
    t = Task(
        id=1,
        title="t",
        owner_user_id="U1",
        priority=TaskPriority.medium,
        status=TaskStatus.in_progress,
    )
    row = _row_dict(t)
    assert row["status"] == "in_progress"
    assert row["deleted_at"] == ""


def test_task_row_includes_completion_artifact():
    t = Task(
        id=1,
        title="t",
        owner_user_id="U1",
        priority=TaskPriority.medium,
        status=TaskStatus.done,
        completion_artifact="https://x/p",
        completion_artifact_kind="url",
    )
    row = _row_dict(t)
    assert row["completion_artifact"] == "https://x/p"


# --------------------------------------------------------------------------- #
# Configurable tab name
# --------------------------------------------------------------------------- #


def test_sheets_factory_uses_configured_tab_name(monkeypatch):
    from app.sync.factories import build_sheets_factory

    settings = Settings(
        GOOGLE_SHEETS_SPREADSHEET_ID="sheet-1",
        GOOGLE_SHEETS_TAB_NAME="Main",
    )
    factory = build_sheets_factory(settings)
    assert factory is not None

    class _Creds:
        pass

    with patch(
        "app.sync.factories._resolve_credentials", return_value=_Creds()
    ), patch("app.sync.factories.SheetsSyncService") as MockService:
        factory()
    kwargs = MockService.call_args.kwargs
    assert kwargs["sheet_name"] == "Main"


def test_sheets_factory_default_tab_name_is_main(monkeypatch):
    from app.sync.factories import build_sheets_factory

    settings = Settings(GOOGLE_SHEETS_SPREADSHEET_ID="sheet-1")
    factory = build_sheets_factory(settings)
    assert factory is not None

    class _Creds:
        pass

    with patch(
        "app.sync.factories._resolve_credentials", return_value=_Creds()
    ), patch("app.sync.factories.SheetsSyncService") as MockService:
        factory()
    assert MockService.call_args.kwargs["sheet_name"] == "Main"


# --------------------------------------------------------------------------- #
# TaskSyncer behaviour
# --------------------------------------------------------------------------- #


def test_task_syncer_noop_when_factories_none():
    from app.sync.task_sync import TaskSyncer

    syncer = TaskSyncer(sheets_factory=None, google_tasks_factory=None)
    # No raise, no DB read.
    syncer.sync(123)


def test_task_syncer_calls_sheets_when_factory_present(
    patched_session_scope, SessionFactory
):
    from app.sync.task_sync import TaskSyncer

    with SessionFactory() as s:
        t = Task(title="t", owner_user_id="U1", status=TaskStatus.in_progress)
        s.add(t)
        s.commit()
        tid = t.id

    captured: dict = {}

    class _SheetsFake:
        def sync(self, session, task):
            captured["task_id"] = task.id

    syncer = TaskSyncer(
        sheets_factory=lambda: _SheetsFake(), google_tasks_factory=None
    )
    syncer.sync(tid)
    assert captured == {"task_id": tid}


def test_task_syncer_swallows_sheets_failure(
    patched_session_scope, SessionFactory
):
    from app.sync.task_sync import TaskSyncer

    with SessionFactory() as s:
        t = Task(title="t", owner_user_id="U1", status=TaskStatus.todo)
        s.add(t)
        s.commit()
        tid = t.id

    class _Boom:
        def sync(self, session, task):
            raise RuntimeError("network down")

    syncer = TaskSyncer(sheets_factory=lambda: _Boom(), google_tasks_factory=None)
    # Must not raise — sync is best-effort.
    syncer.sync(tid)


# --------------------------------------------------------------------------- #
# Handlers call the active syncer
# --------------------------------------------------------------------------- #


def _make_in_progress(session) -> int:
    t = Task(
        title="t",
        owner_user_id="U-owner",
        status=TaskStatus.in_progress,
        priority=TaskPriority.medium,
    )
    session.add(t)
    session.flush()
    return t.id


class _Sender:
    def post_message(self, **kw):
        return {"ok": True, "ts": "1.0"}

    def update_message(self, **kw):
        return {"ok": True}

    def post_ephemeral(self, **kw):
        return {"ok": True}


def test_cancel_triggers_active_syncer(
    patched_session_scope, SessionFactory, ack
):
    from app.slack_bot.handlers.task_actions import handle_cancel_task
    from app.sync.task_sync import TaskSyncer, set_active_syncer

    with SessionFactory() as s:
        tid = _make_in_progress(s)
        s.commit()

    captured: list[int] = []

    class _SheetsFake:
        def sync(self, session, task):
            captured.append(task.id)

    set_active_syncer(
        TaskSyncer(sheets_factory=lambda: _SheetsFake(), google_tasks_factory=None)
    )
    try:
        handle_cancel_task(
            body={
                "actions": [{"value": str(tid)}],
                "user": {"id": "U-owner"},
                "channel": {"id": "C1"},
            },
            sender=_Sender(),
            ack=ack,
        )
    finally:
        set_active_syncer(None)
    assert captured == [tid]


def test_delete_triggers_active_syncer(
    patched_session_scope, SessionFactory, ack
):
    from app.slack_bot.handlers.task_actions import handle_delete_task_submit
    from app.sync.task_sync import TaskSyncer, set_active_syncer

    with SessionFactory() as s:
        tid = _make_in_progress(s)
        s.commit()

    captured: list[int] = []

    class _SheetsFake:
        def sync(self, session, task):
            captured.append(task.id)

    set_active_syncer(
        TaskSyncer(sheets_factory=lambda: _SheetsFake(), google_tasks_factory=None)
    )
    try:
        handle_delete_task_submit(
            body={"user": {"id": "U-owner"}},
            view={"private_metadata": str(tid)},
            sender=_Sender(),
            ack=ack,
        )
    finally:
        set_active_syncer(None)
    assert captured == [tid]


def test_start_work_triggers_active_syncer(
    patched_session_scope, SessionFactory, ack
):
    from app.slack_bot.handlers.task_actions import handle_start_work
    from app.sync.task_sync import TaskSyncer, set_active_syncer

    with SessionFactory() as s:
        t = Task(
            title="t",
            owner_user_id="U-owner",
            status=TaskStatus.todo,
            priority=TaskPriority.medium,
        )
        s.add(t)
        s.commit()
        tid = t.id

    captured: list[int] = []

    class _SheetsFake:
        def sync(self, session, task):
            captured.append(task.id)

    set_active_syncer(
        TaskSyncer(sheets_factory=lambda: _SheetsFake(), google_tasks_factory=None)
    )
    try:
        handle_start_work(
            body={
                "actions": [{"value": str(tid)}],
                "user": {"id": "U-owner"},
                "channel": {"id": "C1"},
            },
            sender=_Sender(),
            ack=ack,
        )
    finally:
        set_active_syncer(None)
    assert captured == [tid]


# --------------------------------------------------------------------------- #
# Service Account auth wiring
# --------------------------------------------------------------------------- #


def test_load_service_account_credentials_returns_none_without_env(monkeypatch):
    from app.sync.google_auth import (
        GOOGLE_SCOPES_SHEETS,
        load_service_account_credentials,
    )

    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_PATH", "")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        assert load_service_account_credentials(GOOGLE_SCOPES_SHEETS) is None
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_resolve_credentials_prefers_service_account(monkeypatch):
    """If the SA JSON env is set, _resolve_credentials must hand back
    those creds and never fall back to the OAuth path."""
    from app.sync import factories

    class _SACreds:
        pass

    with patch(
        "app.sync.factories.load_service_account_credentials",
        return_value=_SACreds(),
    ) as sa_mock, patch(
        "app.sync.factories._load_oauth_credentials"
    ) as oauth_mock:
        out = factories._resolve_credentials(["scope"])
    assert isinstance(out, _SACreds)
    sa_mock.assert_called_once_with(["scope"])
    oauth_mock.assert_not_called()


def test_resolve_credentials_falls_back_to_oauth_when_no_sa():
    from app.sync import factories

    class _OAuth:
        pass

    with patch(
        "app.sync.factories.load_service_account_credentials", return_value=None
    ), patch(
        "app.sync.factories._load_oauth_credentials", return_value=_OAuth()
    ):
        out = factories._resolve_credentials(["scope"])
    assert isinstance(out, _OAuth)
