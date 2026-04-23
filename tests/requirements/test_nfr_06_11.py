"""Tests for NFR-6..NFR-11:

- NFR-6: Mention flow returns draft fast (ack first, no external calls before ack).
- NFR-7: All extracted fields editable before commit.
- NFR-8: Shortcut acked immediately.
- NFR-9: Modal opened with a valid trigger_id used right away.
- NFR-10: External sync ops idempotent and retry-safe.
- NFR-11: Outbound Slack messages go through a rate-aware sender with
         Retry-After handling.
"""
from __future__ import annotations

import json
import time
from typing import Any

import pytest
from slack_sdk.errors import SlackApiError

from app.models import (
    ActionDraft,
    ActionDraftState,
    GoogleSheetsSync,
    GoogleTasksSync,
    SyncStatus,
    Task,
)
from app.slack_bot import blocks as bk


# =============================================================================
# NFR-6: Mention flow is fast — ack precedes any external work.
# =============================================================================


def test_nfr6_mention_calls_ack_before_sender(
    patched_session_scope, services_task, bolt_context, slack_client
):
    from app.slack_bot.handlers.events import handle_app_mention

    order: list[str] = []

    class TrackingSender:
        def post_message(self, **_kw):
            order.append("post")
            return {"ok": True}

    class AckOrder:
        def __call__(self, *_a, **_kw):
            order.append("ack")

    handle_app_mention(
        event={
            "ts": "1.0",
            "user": "U1",
            "text": "<@UBOT> create task",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Nfr6-1"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=TrackingSender(),
        ack=AckOrder(),
    )
    assert order[0] == "ack"


def test_nfr6_mention_ack_called_even_for_silent_result(
    patched_session_scope, services_silent, sender, bolt_context, slack_client
):
    from app.slack_bot.handlers.events import handle_app_mention

    class Ack:
        def __init__(self):
            self.n = 0

        def __call__(self, *a, **kw):
            self.n += 1

    a = Ack()
    handle_app_mention(
        event={
            "ts": "2.0",
            "user": "U1",
            "text": "<@UBOT> blah",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Nfr6-2"},
        client=slack_client,
        context=bolt_context,
        services=services_silent,
        sender=sender,
        ack=a,
    )
    assert a.n == 1


def test_nfr6_mention_sync_path_is_not_blocked_by_external_calls(
    patched_session_scope, services_task, sender, bolt_context, slack_client
):
    """Stand-in: the handler does not call chat_postMessage directly; all
    outbound messages go through the injected sender. This keeps the hot
    path free of blocking network I/O on Slack."""
    from app.slack_bot.handlers.events import handle_app_mention

    handle_app_mention(
        event={
            "ts": "3.0",
            "user": "U1",
            "text": "<@UBOT> x",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Nfr6-3"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=sender,
        ack=lambda *a, **kw: None,
    )
    # slack_client only receives the permalink lookup; chat.postMessage was routed via sender.
    assert slack_client.posted_messages == []


# =============================================================================
# NFR-7: Extracted fields are editable before commit.
# =============================================================================


def test_nfr7_draft_card_has_edit_button():
    from app.schemas.intent import IntentClassification, IntentType, TaskDraft

    c = IntentClassification(
        intent=IntentType.create_task, confidence=0.9, task=TaskDraft(title="t")
    )
    blocks = bk.draft_card(classification=c, draft_id=1, confidence_bucket="high")
    ids = [el["action_id"] for b in blocks if b["type"] == "actions" for el in b["elements"]]
    assert bk.ACTION_EDIT in ids


def test_nfr7_edit_handler_opens_modal_prefilled(
    patched_session_scope, SessionFactory, slack_client
):
    from app.slack_bot.handlers.actions import handle_edit
    from app.models.intent import IntentType as IE
    from tests.test_persistence import _make_draft

    with SessionFactory() as s:
        d = _make_draft(
            s,
            intent=IE.create_task,
            payload={"title": "Prefill", "priority": "high"},
        )
        s.commit()
        did = d.id

    handle_edit(
        body={
            "actions": [{"value": str(did)}],
            "trigger_id": "trg",
            "message": {
                "metadata": {
                    "event_payload": {
                        "metadata": json.dumps(
                            {
                                "conversation_id": "C1",
                                "message_ts": "1.0",
                                "thread_ts": None,
                                "context_snapshot_id": 1,
                                "source_user_id": "U1",
                                "permalink": "p",
                            }
                        )
                    }
                }
            },
        },
        client=slack_client,
        ack=lambda *a, **kw: None,
    )
    view = slack_client.views_opened[0]["view"]
    title_block = next(b for b in view["blocks"] if b["block_id"] == bk.BLOCK_TITLE)
    assert title_block["element"]["initial_value"] == "Prefill"


def test_nfr7_modal_submit_updates_draft_payload(
    patched_session_scope, SessionFactory, sender, finalizer_stub
):
    from app.slack_bot.handlers.views import handle_task_modal_submit
    from app.models.intent import IntentType as IE
    from tests.test_persistence import _make_draft

    with SessionFactory() as s:
        d = _make_draft(s, intent=IE.create_task, payload={"title": "old"})
        s.commit()
        did = d.id

    view = {
        "state": {
            "values": {
                bk.BLOCK_TITLE: {bk.INPUT_TITLE: {"value": "updated title"}},
                bk.BLOCK_DESCRIPTION: {bk.INPUT_DESCRIPTION: {"value": "desc"}},
                bk.BLOCK_OWNER: {bk.INPUT_OWNER: {"value": "alice"}},
                bk.BLOCK_PRIORITY: {bk.INPUT_PRIORITY: {"selected_option": {"value": "urgent"}}},
                bk.BLOCK_DUE: {bk.INPUT_DUE: {"selected_date": "2026-06-01"}},
            }
        },
        "private_metadata": json.dumps(
            {"draft_id": did, "conversation_id": "C1", "message_ts": "1.0"}
        ),
    }
    handle_task_modal_submit(
        body={},
        view=view,
        services=None,
        finalizer=finalizer_stub,
        sender=sender,
        ack=lambda *a, **kw: None,
    )
    with SessionFactory() as s:
        d = s.get(ActionDraft, did)
        assert d.payload["title"] == "updated title"
        assert d.payload["priority"] == "urgent"
        assert d.state in (ActionDraftState.edited, ActionDraftState.confirmed)


@pytest.mark.parametrize(
    "field, block, action_id",
    [
        ("title", bk.BLOCK_TITLE, bk.INPUT_TITLE),
        ("description", bk.BLOCK_DESCRIPTION, bk.INPUT_DESCRIPTION),
        ("owner", bk.BLOCK_OWNER, bk.INPUT_OWNER),
        ("priority", bk.BLOCK_PRIORITY, bk.INPUT_PRIORITY),
        ("due", bk.BLOCK_DUE, bk.INPUT_DUE),
    ],
)
def test_nfr7_task_modal_exposes_every_field(field, block, action_id):
    view = bk.task_modal(private_metadata="{}")
    blocks = [b for b in view["blocks"] if b["block_id"] == block]
    assert blocks, f"missing input for {field}"
    assert blocks[0]["element"]["action_id"] == action_id


@pytest.mark.parametrize(
    "field, block, action_id",
    [
        ("title", bk.BLOCK_TITLE, bk.INPUT_TITLE),
        ("participants", bk.BLOCK_PARTICIPANTS, bk.INPUT_PARTICIPANTS),
        ("datetime", bk.BLOCK_DATETIME, bk.INPUT_DATETIME),
        ("notes", bk.BLOCK_NOTES, bk.INPUT_NOTES),
    ],
)
def test_nfr7_meeting_modal_exposes_every_field(field, block, action_id):
    view = bk.meeting_modal(private_metadata="{}")
    blocks = [b for b in view["blocks"] if b["block_id"] == block]
    assert blocks, f"missing input for {field}"
    assert blocks[0]["element"]["action_id"] == action_id


# =============================================================================
# NFR-8: Shortcut is acked immediately.
# =============================================================================


def test_nfr8_shortcut_handler_calls_ack(patched_session_scope, services_task, ack, slack_client):
    from app.slack_bot.handlers.shortcuts import SHORTCUT_CREATE_TASK, handle_shortcut

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
        ack=ack,
    )
    assert ack.called


def test_nfr8_shortcut_acks_even_without_message(
    patched_session_scope, services_task, ack, slack_client
):
    from app.slack_bot.handlers.shortcuts import SHORTCUT_CREATE_TASK, handle_shortcut

    handle_shortcut(
        shortcut={
            "callback_id": SHORTCUT_CREATE_TASK,
            "trigger_id": "t",
            "channel": {"id": "C1"},
            "user": {"id": "U1"},
        },
        client=slack_client,
        services=services_task,
        ack=ack,
    )
    assert ack.called


def test_nfr8_shortcut_acks_even_without_trigger_id(
    patched_session_scope, services_task, ack, slack_client
):
    from app.slack_bot.handlers.shortcuts import SHORTCUT_CREATE_TASK, handle_shortcut

    handle_shortcut(
        shortcut={
            "callback_id": SHORTCUT_CREATE_TASK,
            "channel": {"id": "C1"},
            "user": {"id": "U1"},
        },
        client=slack_client,
        services=services_task,
        ack=ack,
    )
    assert ack.called


# =============================================================================
# NFR-9: Modal opened with valid trigger_id used right away.
# =============================================================================


def test_nfr9_views_open_receives_trigger_id_from_shortcut(
    patched_session_scope, services_task, ack, slack_client
):
    from app.slack_bot.handlers.shortcuts import SHORTCUT_CREATE_TASK, handle_shortcut

    handle_shortcut(
        shortcut={
            "callback_id": SHORTCUT_CREATE_TASK,
            "trigger_id": "TRIG-XYZ",
            "channel": {"id": "C1"},
            "user": {"id": "U1"},
            "message": {"ts": "1.0", "user": "U1", "text": "x"},
        },
        client=slack_client,
        services=services_task,
        ack=ack,
    )
    assert slack_client.views_opened[0]["trigger_id"] == "TRIG-XYZ"


def test_nfr9_edit_button_uses_button_trigger_id(
    patched_session_scope, SessionFactory, slack_client
):
    from app.slack_bot.handlers.actions import handle_edit
    from app.models.intent import IntentType as IE
    from tests.test_persistence import _make_draft

    with SessionFactory() as s:
        d = _make_draft(s, intent=IE.create_task, payload={"title": "x"})
        s.commit()
        did = d.id

    handle_edit(
        body={
            "actions": [{"value": str(did)}],
            "trigger_id": "EditTrig",
            "message": {"metadata": {}},
        },
        client=slack_client,
        ack=lambda *a, **kw: None,
    )
    assert slack_client.views_opened[0]["trigger_id"] == "EditTrig"


def test_nfr9_missing_trigger_id_skips_views_open(
    patched_session_scope, SessionFactory, slack_client
):
    from app.slack_bot.handlers.actions import handle_edit
    from app.models.intent import IntentType as IE
    from tests.test_persistence import _make_draft

    with SessionFactory() as s:
        d = _make_draft(s, intent=IE.create_task, payload={"title": "x"})
        s.commit()
        did = d.id

    handle_edit(
        body={"actions": [{"value": str(did)}], "message": {"metadata": {}}},
        client=slack_client,
        ack=lambda *a, **kw: None,
    )
    assert slack_client.views_opened == []


def test_nfr9_shortcut_with_empty_trigger_id_skips_views_open(
    patched_session_scope, services_task, ack, slack_client
):
    from app.slack_bot.handlers.shortcuts import SHORTCUT_CREATE_TASK, handle_shortcut

    handle_shortcut(
        shortcut={
            "callback_id": SHORTCUT_CREATE_TASK,
            "trigger_id": "",
            "channel": {"id": "C1"},
            "user": {"id": "U1"},
            "message": {"ts": "1.0", "user": "U1", "text": "x"},
        },
        client=slack_client,
        services=services_task,
        ack=ack,
    )
    assert slack_client.views_opened == []


# =============================================================================
# NFR-10: External sync operations are idempotent and retry-safe.
# =============================================================================


def _fake_resp(status: int):
    # googleapiclient.errors.HttpError expects .status and .reason on resp.
    class R:
        pass

    r = R()
    r.status = status
    r.reason = "boom"
    return r


class _FakeSheetsApi:
    def __init__(self, raise_on_call: int | None = None) -> None:
        self.append_calls = 0
        self.update_calls = 0
        self._raise_on = raise_on_call

    def _maybe_raise(self, n):
        if self._raise_on is not None and n == self._raise_on:
            from googleapiclient.errors import HttpError

            raise HttpError(
                resp=type("R", (), {"status": 500, "reason": "boom"})(),
                content=b"transient",
            )

    def append(self, row):
        self.append_calls += 1
        self._maybe_raise(self.append_calls)
        return {"updates": {"updatedRange": f"Tasks!A{self.append_calls + 10}:J{self.append_calls + 10}"}}

    def update(self, row_id, row):
        self.update_calls += 1
        return {"updatedRange": f"Tasks!A{row_id}:J{row_id}"}


class _PatchedSheetsService:
    """Sheet sync service with internals replaced by fakes."""

    def __init__(self, api: _FakeSheetsApi, spreadsheet_id="SHEET"):
        from app.sync.sheets import SheetsSyncService

        # Construct without touching Google; then swap internals.
        svc = SheetsSyncService.__new__(SheetsSyncService)
        svc._service = None  # not used; we patch the methods below
        svc._spreadsheet_id = spreadsheet_id
        svc._sheet_name = "Tasks"
        svc._append = api.append
        svc._update = api.update
        self.svc = svc


def _insert_task_row(session):
    from app.models.task import TaskPriority, TaskStatus

    t = Task(title="x", priority=TaskPriority.medium, status=TaskStatus.todo)
    session.add(t)
    session.flush()
    return t


def test_nfr10_sheets_sync_appends_once_and_updates_after(session):
    api = _FakeSheetsApi()
    svc = _PatchedSheetsService(api).svc
    t = _insert_task_row(session)

    svc.sync(session, t)
    svc.sync(session, t)

    assert api.append_calls == 1  # only first time
    assert api.update_calls == 1  # subsequent sync updates the same row
    rec = session.query(GoogleSheetsSync).filter_by(task_id=t.id).one()
    assert rec.status == SyncStatus.success
    assert rec.row_id is not None


def test_nfr10_sheets_sync_records_failure_and_increments_attempts(session):
    from googleapiclient.errors import HttpError

    api = _FakeSheetsApi(raise_on_call=1)

    class AlwaysFail:
        def append(self, row):
            raise HttpError(resp=_fake_resp(500), content=b"boom")

        def update(self, row_id, row):
            raise HttpError(resp=_fake_resp(500), content=b"boom")

    from app.sync.sheets import SheetsSyncService

    svc = SheetsSyncService.__new__(SheetsSyncService)
    svc._service = None
    svc._spreadsheet_id = "SHEET"
    svc._sheet_name = "Tasks"
    svc._append = AlwaysFail().append
    svc._update = AlwaysFail().update

    t = _insert_task_row(session)
    with pytest.raises(HttpError):
        svc.sync(session, t)
    rec = session.query(GoogleSheetsSync).filter_by(task_id=t.id).one()
    assert rec.status == SyncStatus.failed
    assert rec.attempts == 1
    assert rec.last_error


def test_nfr10_google_tasks_inserts_once_and_patches_next(session):
    calls = {"insert": 0, "patch": 0}

    class FakeSvc:
        def insert(self, body):
            calls["insert"] += 1
            return {"id": "gtask-1"}

        def patch(self, google_task_id, body):
            calls["patch"] += 1
            return {"id": google_task_id}

    from app.sync.tasks_api import GoogleTasksSyncService

    svc = GoogleTasksSyncService.__new__(GoogleTasksSyncService)
    svc._service = None
    svc._tasklist_id = "@default"
    svc._google_user_id = None
    fake = FakeSvc()
    svc._insert = fake.insert
    svc._patch = fake.patch

    t = _insert_task_row(session)
    svc.sync(session, t)
    svc.sync(session, t)

    assert calls["insert"] == 1
    assert calls["patch"] == 1
    rec = session.query(GoogleTasksSync).filter_by(task_id=t.id).one()
    assert rec.google_task_id == "gtask-1"
    assert rec.status == SyncStatus.success
    assert rec.attempts == 2


def test_nfr10_google_tasks_sync_records_failure(session):
    from googleapiclient.errors import HttpError

    class FakeSvc:
        def insert(self, body):
            raise HttpError(resp=_fake_resp(500), content=b"x")

        def patch(self, google_task_id, body):
            raise HttpError(resp=_fake_resp(500), content=b"x")

    from app.sync.tasks_api import GoogleTasksSyncService

    svc = GoogleTasksSyncService.__new__(GoogleTasksSyncService)
    svc._service = None
    svc._tasklist_id = "@default"
    svc._google_user_id = None
    fake = FakeSvc()
    svc._insert = fake.insert
    svc._patch = fake.patch

    t = _insert_task_row(session)
    with pytest.raises(HttpError):
        svc.sync(session, t)
    rec = session.query(GoogleTasksSync).filter_by(task_id=t.id).one()
    assert rec.status == SyncStatus.failed
    assert rec.last_error


def test_nfr10_sheets_parse_row_id_handles_ranges():
    from app.sync.sheets import _parse_row_id

    assert _parse_row_id("Tasks!A12:J12") == 12
    assert _parse_row_id("Sheet1!B7:E7") == 7


def test_nfr10_sheets_parse_row_id_returns_none_for_invalid():
    from app.sync.sheets import _parse_row_id

    assert _parse_row_id("") is None
    assert _parse_row_id("garbage") is None


def test_nfr10_sync_failure_does_not_block_db_persistence(
    patched_session_scope, SessionFactory
):
    from app.config import Settings
    from app.orchestrator.finalize import FinalizeService
    from tests.requirements.test_fr_11_12_persistence import _prep

    class BoomService:
        def sync(self, session, task):
            raise RuntimeError("boom")

    with SessionFactory() as s:
        draft, snap = _prep(s)
        s.commit()
        draft_id, snap_id = draft.id, snap.id

    fin = FinalizeService(
        settings=Settings(),
        sheets_service_factory=lambda: BoomService(),
        google_tasks_service_factory=lambda: BoomService(),
    )
    entity_type, entity_id, _ = fin.finalize_draft(
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
        assert s.query(Task).count() == 1
    assert entity_type == "task"


# =============================================================================
# NFR-11: Outbound Slack messages go through rate-aware sender with
#         Retry-After handling.
# =============================================================================


class _FakePostResp:
    def __init__(self, ok=True):
        self.data = {"ok": ok, "ts": "0.0"}


class _RateLimitError(SlackApiError):
    def __init__(self, retry_after: int = 1):
        resp = type(
            "R",
            (),
            {"status_code": 429, "headers": {"Retry-After": str(retry_after)}, "data": {}},
        )()
        super().__init__("rate_limited", resp)


def test_nfr11_rate_sender_throttles_per_channel(monkeypatch):
    from app.slack_bot.rate_limiter import RateAwareSlackSender

    class Cli:
        def chat_postMessage(self, **kw):  # noqa: N802
            return _FakePostResp()

    s = RateAwareSlackSender(Cli(), min_interval_seconds=0.2)

    t0 = time.monotonic()
    s.post_message(channel="C1", text="a")
    s.post_message(channel="C1", text="b")
    t1 = time.monotonic()
    assert (t1 - t0) >= 0.2


def test_nfr11_rate_sender_does_not_throttle_across_channels(monkeypatch):
    from app.slack_bot.rate_limiter import RateAwareSlackSender

    class Cli:
        def chat_postMessage(self, **kw):  # noqa: N802
            return _FakePostResp()

    s = RateAwareSlackSender(Cli(), min_interval_seconds=0.5)
    t0 = time.monotonic()
    s.post_message(channel="C1", text="a")
    s.post_message(channel="C2", text="b")
    t1 = time.monotonic()
    # Different channels → no cross-channel wait required beyond first call.
    assert (t1 - t0) < 0.4


def test_nfr11_retries_on_429_with_retry_after(monkeypatch):
    from app.slack_bot.rate_limiter import RateAwareSlackSender

    calls = {"n": 0}

    class Cli:
        def chat_postMessage(self, **kw):  # noqa: N802
            calls["n"] += 1
            if calls["n"] == 1:
                raise _RateLimitError(retry_after=0)  # no real sleep
            return _FakePostResp()

    s = RateAwareSlackSender(Cli(), min_interval_seconds=0.0)
    s.post_message(channel="C1", text="x")
    assert calls["n"] == 2


def test_nfr11_gives_up_after_three_retries(monkeypatch):
    from app.slack_bot.rate_limiter import RateAwareSlackSender

    calls = {"n": 0}

    class Cli:
        def chat_postMessage(self, **kw):  # noqa: N802
            calls["n"] += 1
            raise _RateLimitError(retry_after=0)

    s = RateAwareSlackSender(Cli(), min_interval_seconds=0.0)
    with pytest.raises(SlackApiError):
        s.post_message(channel="C1", text="x")
    assert calls["n"] >= 3


def test_nfr11_non_429_errors_not_retried():
    from app.slack_bot.rate_limiter import RateAwareSlackSender

    calls = {"n": 0}

    class Cli:
        def chat_postMessage(self, **kw):  # noqa: N802
            calls["n"] += 1
            resp = type("R", (), {"status_code": 500, "headers": {}, "data": {}})()
            raise SlackApiError("server err", resp)

    s = RateAwareSlackSender(Cli(), min_interval_seconds=0.0)
    with pytest.raises(SlackApiError):
        s.post_message(channel="C1", text="x")
    assert calls["n"] == 1  # no retry for non-429


def test_nfr11_returns_payload_on_success():
    from app.slack_bot.rate_limiter import RateAwareSlackSender

    class Cli:
        def chat_postMessage(self, **kw):  # noqa: N802
            return _FakePostResp()

    s = RateAwareSlackSender(Cli(), min_interval_seconds=0.0)
    out = s.post_message(channel="C1", text="x")
    assert out["ok"] is True
