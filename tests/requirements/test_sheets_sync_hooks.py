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
# Owner column: human-readable, never raw "<@Uxxx>" mention
# --------------------------------------------------------------------------- #


def test_owner_resolves_to_real_name_via_employees(session):
    """When the owner is in the employees table, the sheet shows the
    person's real name — not `<@Uxxx>`, not their @username."""
    from app.models import Employee
    from app.sync.sheets import _resolve_owner_name

    session.add(
        Employee(
            slack_user_id="U-andre",
            display_name="admin",  # @username fallback in Slack
            real_name="Andre Kuzminykh",
        )
    )
    session.flush()
    t = Task(
        id=1,
        title="t",
        owner_user_id="U-andre",
        owner_display_name="<@U-andre>",  # set by the quiet-author fallback
        priority=TaskPriority.medium,
        status=TaskStatus.todo,
    )
    assert _resolve_owner_name(session, t) == "Andre Kuzminykh"


def test_owner_strips_slack_mention_when_employee_unknown(session):
    """If the owner isn't in employees yet, fall back to the bare uid
    (stripped of `<@...>` wrapping) rather than dumping the mention."""
    from app.sync.sheets import _resolve_owner_name

    t = Task(
        id=1,
        title="t",
        owner_user_id="U09STRANGER",
        owner_display_name="<@U09STRANGER>",
        priority=TaskPriority.medium,
        status=TaskStatus.todo,
    )
    assert _resolve_owner_name(session, t) == "U09STRANGER"


def test_owner_uses_display_name_when_no_real_name(session):
    from app.models import Employee
    from app.sync.sheets import _resolve_owner_name

    session.add(
        Employee(slack_user_id="U-d", display_name="Dee", real_name=None)
    )
    session.flush()
    t = Task(
        id=1,
        title="t",
        owner_user_id="U-d",
        priority=TaskPriority.medium,
        status=TaskStatus.todo,
    )
    assert _resolve_owner_name(session, t) == "Dee"


def test_owner_returns_owner_user_id_as_last_resort():
    from app.sync.sheets import _resolve_owner_name

    t = Task(
        id=1,
        title="t",
        owner_user_id="U-ghost",
        priority=TaskPriority.medium,
        status=TaskStatus.todo,
    )
    # No session → no employees lookup; no display_name set.
    assert _resolve_owner_name(None, t) == "U-ghost"


# --------------------------------------------------------------------------- #
# Auto-write header row
# --------------------------------------------------------------------------- #


def _make_svc():
    from app.sync.sheets import SheetsSyncService

    svc = SheetsSyncService.__new__(SheetsSyncService)
    svc._service = None  # we'll assign a fake below
    svc._spreadsheet_id = "SHEET"
    svc._sheet_name = "Main"
    svc._headers_checked = False
    return svc


class _FakeSheets:
    """Minimal stub of `service.spreadsheets().values().get/.update()`."""

    def __init__(self, *, current_headers: list | None = None):
        self.gets: list[str] = []
        self.updates: list[dict] = []
        self._current_headers = current_headers

    # The chain `service.spreadsheets().values().get(...).execute()` is
    # implemented as nested objects that all return self where useful.
    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, spreadsheetId, range):  # noqa: N803
        self.gets.append(range)
        self._last_op = (
            "get",
            {"spreadsheetId": spreadsheetId, "range": range},
        )
        return self

    def update(self, spreadsheetId, range, valueInputOption, body):  # noqa: N803
        self.updates.append(
            {
                "spreadsheetId": spreadsheetId,
                "range": range,
                "valueInputOption": valueInputOption,
                "body": body,
            }
        )
        self._last_op = ("update", {})
        return self

    def execute(self):
        if self._last_op[0] == "get":
            return {"values": [self._current_headers]} if self._current_headers else {}
        return {}


def test_ensure_headers_writes_when_row1_empty():
    from app.sync.sheets import _HEADER_ROW

    svc = _make_svc()
    fake = _FakeSheets(current_headers=None)
    svc._service = fake

    svc._ensure_headers()
    assert len(fake.updates) == 1
    upd = fake.updates[0]
    assert upd["range"].startswith("Main!A1:")
    assert upd["body"]["values"][0] == _HEADER_ROW


def test_col_letter_helper_handles_az_and_aa_boundaries():
    """FR-CR-05-86 — `_col_letter` underpins every range string
    we send to Sheets. 22 cols → 'V', 26 → 'Z', 27 → 'AA'."""
    from app.sync.sheets import _col_letter

    assert _col_letter(1) == "A"
    assert _col_letter(22) == "V"
    assert _col_letter(26) == "Z"
    assert _col_letter(27) == "AA"
    assert _col_letter(28) == "AB"
    assert _col_letter(52) == "AZ"
    assert _col_letter(53) == "BA"


def test_append_uses_schema_width_range_not_a_z():
    """FR-CR-05-86 — operator regression: new rows landed
    shifted ~22 columns right because `range='A:Z'` (26 cols)
    plus stray data in W-Z (the rolled-back legacy `dialogue`
    column) made Google's append heuristic detect a
    wider-than-22 «table» and place new rows past the schema.
    The fix pins the range to EXACTLY `len(_HEADER_ROW)` cols."""
    from app.sync.sheets import _HEADER_ROW, _col_letter

    expected_end = _col_letter(len(_HEADER_ROW))
    captured: dict = {}

    class _FakeSvc:
        def spreadsheets(self):
            return self

        def values(self):
            return self

        def append(self, *, spreadsheetId, range, valueInputOption, insertDataOption, body):  # noqa: N803
            captured["range"] = range
            captured["body"] = body
            return self

        def update(self, *, spreadsheetId, range, valueInputOption, body):  # noqa: N803
            captured["update_range"] = range
            return self

        def execute(self):
            return {"updates": {"updatedRange": f"Main!A100:{expected_end}100"}}

    svc = _make_svc()
    svc._service = _FakeSvc()
    svc._headers_checked = True  # skip the get/update flow

    row = ["v"] * len(_HEADER_ROW)
    svc._append(row)
    assert captured["range"] == f"Main!A:{expected_end}"
    # Sanity: the legacy A:Z wide range MUST be gone.
    assert "A:Z" not in captured["range"]


def test_update_uses_schema_width_range_not_a_z():
    """FR-CR-05-86 — same pin on the per-row update range so
    operator edits don't bleed into stray columns past `V`."""
    from app.sync.sheets import _HEADER_ROW, _col_letter

    expected_end = _col_letter(len(_HEADER_ROW))
    captured: dict = {}

    class _FakeSvc:
        def spreadsheets(self):
            return self

        def values(self):
            return self

        def update(self, *, spreadsheetId, range, valueInputOption, body):  # noqa: N803
            captured["range"] = range
            return self

        def execute(self):
            return {}

    svc = _make_svc()
    svc._service = _FakeSvc()
    svc._headers_checked = True

    row = ["v"] * len(_HEADER_ROW)
    svc._update(42, row)
    assert captured["range"] == f"Main!A42:{expected_end}42"
    assert "A42:Z42" not in captured["range"]


def test_ensure_headers_overwrites_when_row1_mismatches():
    """The user's manually-typed headers were a different schema; the
    bot must take over to keep column order in sync with `_task_row`."""
    svc = _make_svc()
    fake = _FakeSheets(
        current_headers=["task_id", "title", "owner"]  # not our shape
    )
    svc._service = fake

    svc._ensure_headers()
    assert len(fake.updates) == 1


def test_ensure_headers_no_op_when_already_correct():
    from app.sync.sheets import _HEADER_ROW

    svc = _make_svc()
    fake = _FakeSheets(current_headers=list(_HEADER_ROW))
    svc._service = fake

    svc._ensure_headers()
    assert fake.updates == []


def test_ensure_headers_runs_at_most_once_per_process():
    svc = _make_svc()
    fake = _FakeSheets(current_headers=None)
    svc._service = fake

    svc._ensure_headers()
    svc._ensure_headers()
    svc._ensure_headers()
    # Only the first call hit the Sheets API; subsequent ones short-circuit.
    assert len(fake.gets) == 1
    assert len(fake.updates) == 1


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


def test_create_task_from_draft_triggers_initial_sheets_sync(
    patched_session_scope, SessionFactory
):
    """FR-CR-04-26 + FR-CR-05-* — every newly created Task lands in
    the sheet right away, regardless of which channel created it
    (Slack orchestrator, Telegram immediate-create, Telegram
    Accept-on-draft). Without this hook the TG path was missing
    rows until a later status change happened to fire a sync."""
    from app.models import ActionDraft, IntentInference
    from app.models.intent import IntentType
    from app.persistence import create_task_from_draft
    from app.sync.task_sync import TaskSyncer, set_active_syncer

    captured: list[int] = []

    class _SheetsFake:
        def sync(self, session, task):
            captured.append(task.id)

    set_active_syncer(
        TaskSyncer(sheets_factory=lambda: _SheetsFake(), google_tasks_factory=None)
    )
    try:
        with SessionFactory() as s:
            inference = IntentInference(
                intent=IntentType.create_task,
                confidence=0.9,
                invocation_type="passive",
            )
            s.add(inference)
            s.flush()
            draft = ActionDraft(
                inference_id=inference.id,
                intent=IntentType.create_task,
                payload={"title": "fresh task", "priority": "medium"},
                created_by_slack_user_id="U-author",
            )
            s.add(draft)
            s.flush()
            task = create_task_from_draft(
                s,
                draft=draft,
                source={"kind": "telegram"},
                context_snapshot_id=None,
                fallback_author_slack_id="U-author",
            )
            s.commit()
            assert captured == [task.id]
    finally:
        set_active_syncer(None)


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
