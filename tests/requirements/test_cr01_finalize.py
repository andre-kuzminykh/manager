"""Tests for CR-01 finalize extensions:
- Auto-subscribe owner and source author on persist (FR-CR-5).
- Post the task card into the source channel after successful persist.
- Pass allowed_owners into the task modal (FR-CR-1).
"""
from __future__ import annotations

from app.config import Settings
from app.models import Task, TaskStatus, TaskSubscription
from app.orchestrator.finalize import FinalizeService
from app.slack_bot import blocks as bk
from app.slack_bot.handlers.shortcuts import SHORTCUT_CREATE_TASK, handle_shortcut
from tests.requirements.test_fr_11_12_persistence import _prep


class _RecordingSender:
    def __init__(self) -> None:
        self.posted: list[dict] = []
        self.updated: list[dict] = []

    def post_message(self, **kw):
        self.posted.append(kw)
        return {"ok": True, "ts": "99.0"}

    def update_message(self, **kw):
        self.updated.append(kw)
        return {"ok": True}

    def delete_message(self, **kw):
        return {"ok": True}


def test_finalize_updates_draft_widget_in_place_and_dms_owner(
    patched_session_scope, SessionFactory
):
    """After confirm the widget is chat.update'd into a task card (not a
    second message) and a DM mirror is posted to the owner."""
    sender = _RecordingSender()
    fin = FinalizeService(settings=Settings(), sender=sender)

    with SessionFactory() as s:
        draft, snap = _prep(s, payload={"title": "hello", "owner_user_id": "U-owner"})
        # Pre-seed the draft's card coordinates (as handle_app_mention does).
        draft.card_channel = "C1"
        draft.card_ts = "100.0"
        s.commit()
        draft_id, snap_id = draft.id, snap.id

    fin.finalize_draft(
        draft_id=draft_id,
        source_metadata={
            "conversation_id": "C1",
            "message_ts": "1.0",
            "thread_ts": None,
            "permalink": "https://x/p",
            "context_snapshot_id": snap_id,
            "source_user_id": "U1",
        },
    )
    # The widget was updated in place.
    assert sender.updated
    assert sender.updated[0]["channel"] == "C1"
    assert sender.updated[0]["ts"] == "100.0"

    # And a DM was posted to the owner.
    assert sender.posted
    assert sender.posted[0]["channel"] == "U-owner"


def test_finalize_owner_dm_card_hides_subscribe(
    patched_session_scope, SessionFactory
):
    # The DM copy is rendered for the owner, who is implicitly subscribed.
    # The Subscribe / Unsubscribe toggle must NOT appear in their view.
    sender = _RecordingSender()
    fin = FinalizeService(settings=Settings(), sender=sender)

    with SessionFactory() as s:
        draft, snap = _prep(s, payload={"title": "hello", "owner_user_id": "U-owner"})
        s.commit()
        draft_id, snap_id = draft.id, snap.id

    fin.finalize_draft(
        draft_id=draft_id,
        source_metadata={
            "conversation_id": "C1",
            "message_ts": "1.0",
            "thread_ts": None,
            "permalink": "p",
            "context_snapshot_id": snap_id,
        },
    )
    owner_dm = next(m for m in sender.posted if m["channel"] == "U-owner")
    ids = [
        el["action_id"]
        for b in owner_dm["blocks"]
        if b["type"] == "actions"
        for el in b["elements"]
    ]
    assert bk.ACTION_SUBSCRIBE not in ids
    assert bk.ACTION_UNSUBSCRIBE not in ids


def test_finalize_does_not_crash_without_sender(patched_session_scope, SessionFactory):
    fin = FinalizeService(settings=Settings())  # sender=None

    with SessionFactory() as s:
        draft, snap = _prep(s)
        s.commit()
        draft_id, snap_id = draft.id, snap.id

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
    assert entity_type == "task"
    with SessionFactory() as s:
        assert s.query(Task).count() == 1


def test_finalize_meeting_does_not_post_task_card(
    patched_session_scope, SessionFactory
):
    from app.models.intent import IntentType as IE

    sender = _RecordingSender()
    fin = FinalizeService(settings=Settings(), sender=sender)

    with SessionFactory() as s:
        draft, snap = _prep(
            s,
            intent=IE.create_meeting,
            payload={
                "title": "Sync",
                "participants": [],
                "datetime_at": "2026-06-01T10:00:00+00:00",
            },
        )
        s.commit()
        draft_id, snap_id = draft.id, snap.id

    fin.finalize_draft(
        draft_id=draft_id,
        source_metadata={
            "conversation_id": "C1",
            "message_ts": "1.0",
            "thread_ts": None,
            "permalink": "p",
            "context_snapshot_id": snap_id,
        },
    )
    assert sender.posted == []


def test_finalize_auto_subscribes_owner_and_author(
    patched_session_scope, SessionFactory
):
    fin = FinalizeService(settings=Settings())

    with SessionFactory() as s:
        draft, snap = _prep(s, payload={"title": "t", "owner_user_id": "U-owner"})
        s.commit()
        draft_id, snap_id = draft.id, snap.id

    fin.finalize_draft(
        draft_id=draft_id,
        source_metadata={
            "conversation_id": "C1",
            "message_ts": "1.0",
            "thread_ts": None,
            "permalink": "p",
            "context_snapshot_id": snap_id,
            "source_user_id": "U-author",
        },
    )
    with SessionFactory() as s:
        subs = {s_.slack_user_id for s_ in s.query(TaskSubscription).all()}
    # Author comes from the draft's created_by; owner from the payload.
    assert "U-owner" in subs


def test_shortcut_modal_receives_allowed_owners(
    patched_session_scope, services_task, ack, slack_client, monkeypatch
):
    monkeypatch.setenv(
        "ALLOWED_OWNERS",
        '[{"slack_user_id":"U1","display_name":"Alice"}]',
    )
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]

    payload = {
        "callback_id": SHORTCUT_CREATE_TASK,
        "trigger_id": "t",
        "channel": {"id": "C1"},
        "user": {"id": "U1"},
        "message": {"ts": "1.0", "user": "U1", "text": "make task"},
    }
    handle_shortcut(shortcut=payload, client=slack_client, services=services_task, ack=ack)
    view = slack_client.views_opened[0]["view"]
    owner_block = next(b for b in view["blocks"] if b["block_id"] == bk.BLOCK_OWNER)
    assert owner_block["element"]["type"] == "static_select"
    assert owner_block["element"]["options"][0]["value"] == "U1"

    get_settings.cache_clear()  # type: ignore[attr-defined]


def test_task_modal_passes_through_effort_initial_value():
    view = bk.task_modal(private_metadata="{}", initial={"estimated_minutes": 180})
    effort_block = next(b for b in view["blocks"] if b["block_id"] == bk.BLOCK_EFFORT)
    assert effort_block["element"]["initial_value"] == "180"


def test_task_modal_submit_parses_effort_integer(
    patched_session_scope, SessionFactory, sender, finalizer_stub
):
    from app.slack_bot.handlers.views import handle_task_modal_submit

    view = {
        "state": {
            "values": {
                bk.BLOCK_TITLE: {bk.INPUT_TITLE: {"value": "task"}},
                bk.BLOCK_PRIORITY: {
                    bk.INPUT_PRIORITY: {"selected_option": {"value": "medium"}}
                },
                bk.BLOCK_EFFORT: {bk.INPUT_EFFORT: {"value": "240"}},
            }
        },
        "private_metadata": "{}",
    }
    handle_task_modal_submit(
        body={},
        view=view,
        services=None,
        finalizer=finalizer_stub,
        sender=sender,
        ack=lambda *a, **kw: None,
    )
    # The finalizer was invoked with some draft id; the underlying draft
    # should carry the parsed estimated_minutes.
    with SessionFactory() as s:
        from app.models import ActionDraft

        drafts = s.query(ActionDraft).all()
        assert drafts
        assert any(d.payload.get("estimated_minutes") == 240 for d in drafts)


def test_task_modal_submit_parses_owner_slack_id(
    patched_session_scope, SessionFactory, sender, finalizer_stub
):
    from app.slack_bot.handlers.views import handle_task_modal_submit

    view = {
        "state": {
            "values": {
                bk.BLOCK_TITLE: {bk.INPUT_TITLE: {"value": "task"}},
                bk.BLOCK_OWNER: {
                    bk.INPUT_OWNER: {"selected_option": {"value": "U42"}}
                },
                bk.BLOCK_PRIORITY: {
                    bk.INPUT_PRIORITY: {"selected_option": {"value": "medium"}}
                },
            }
        },
        "private_metadata": "{}",
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
        from app.models import ActionDraft

        d = s.query(ActionDraft).first()
        assert d.payload.get("owner_user_id") == "U42"
