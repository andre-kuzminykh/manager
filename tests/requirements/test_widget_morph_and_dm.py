"""Requirement coverage: FR-CR-02-3 (chat.update on reply), FR-CR-02-4
(draft widget disappears on Confirm / Ignore), FR-CR-03-10 (live
admin updates via chat.update).

CR-03 widget lifecycle:
- draft widget morphs in place via chat.update on Confirm
- task card is also mirrored to the owner's DM (running log)
- Start работу shows for owner OR when no owner assigned
- follow-up Q&A messages are deleted on Confirm / Ignore
- task card updates in place on status changes"""
from __future__ import annotations

from types import SimpleNamespace

from app.models import ActionDraft, ActionDraftState, Task, TaskStatus
from app.models.intent import IntentType as IE
from app.slack_bot import blocks as bk


# --------------------------------------------------------------------------- #
# "Начать работу" visibility
# --------------------------------------------------------------------------- #


def test_start_work_visible_for_owner(session):
    t = Task(title="t", owner_user_id="U-owner", status=TaskStatus.todo)
    session.add(t)
    session.flush()
    blocks = bk.task_card(task=t, viewer_slack_user_id="U-owner")
    ids = [
        el["action_id"]
        for b in blocks
        if b["type"] == "actions"
        for el in b["elements"]
    ]
    assert bk.ACTION_START_WORK in ids


def test_start_work_visible_when_no_owner_assigned(session):
    t = Task(title="t", owner_user_id=None, status=TaskStatus.todo)
    session.add(t)
    session.flush()
    blocks = bk.task_card(task=t, viewer_slack_user_id="U-random")
    ids = [
        el["action_id"]
        for b in blocks
        if b["type"] == "actions"
        for el in b["elements"]
    ]
    assert bk.ACTION_START_WORK in ids


def test_start_work_hidden_for_non_owner(session):
    t = Task(title="t", owner_user_id="U-someone", status=TaskStatus.todo)
    session.add(t)
    session.flush()
    blocks = bk.task_card(task=t, viewer_slack_user_id="U-other")
    ids = [
        el["action_id"]
        for b in blocks
        if b["type"] == "actions"
        for el in b["elements"]
    ]
    assert bk.ACTION_START_WORK not in ids


# --------------------------------------------------------------------------- #
# Widget morph: chat.update + DM mirror
# --------------------------------------------------------------------------- #


class _DualSender:
    def __init__(self):
        self.updates: list[dict] = []
        self.posts: list[dict] = []
        self.deletes: list[dict] = []
        self._ts = 100

    def update_message(self, **kw):
        self.updates.append(kw)
        return {"ok": True}

    def post_message(self, **kw):
        self._ts += 1
        self.posts.append(kw)
        return {"ok": True, "ts": f"{self._ts}.0"}

    def delete_message(self, **kw):
        self.deletes.append(kw)
        return {"ok": True}


def test_finalize_morphs_widget_in_place(patched_session_scope, SessionFactory):
    from app.config import Settings
    from app.orchestrator.finalize import FinalizeService
    from tests.requirements.test_fr_11_12_persistence import _prep

    sender = _DualSender()
    fin = FinalizeService(settings=Settings(), sender=sender)

    with SessionFactory() as s:
        draft, snap = _prep(s, payload={"title": "t", "owner_user_id": "U-owner"})
        draft.card_channel = "C1"
        draft.card_ts = "500.0"
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
    # The original widget was chat.update'd, not deleted.
    assert sender.updates
    assert sender.updates[0]["channel"] == "C1"
    assert sender.updates[0]["ts"] == "500.0"
    assert sender.deletes == []


def test_finalize_stores_card_coordinates_on_task(
    patched_session_scope, SessionFactory
):
    from app.config import Settings
    from app.orchestrator.finalize import FinalizeService
    from tests.requirements.test_fr_11_12_persistence import _prep

    sender = _DualSender()
    fin = FinalizeService(settings=Settings(), sender=sender)

    with SessionFactory() as s:
        draft, snap = _prep(s, payload={"title": "t", "owner_user_id": "U-owner"})
        draft.card_channel = "C1"
        draft.card_ts = "500.0"
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
    with SessionFactory() as s:
        task = s.query(Task).one()
        assert task.card_channel == "C1"
        assert task.card_ts == "500.0"
        # DM mirror posted to owner — ts captured too.
        assert task.dm_channel == "U-owner"
        assert task.dm_ts is not None


def test_finalize_dms_owner_with_task_card(patched_session_scope, SessionFactory):
    from app.config import Settings
    from app.orchestrator.finalize import FinalizeService
    from tests.requirements.test_fr_11_12_persistence import _prep

    sender = _DualSender()
    fin = FinalizeService(settings=Settings(), sender=sender)

    with SessionFactory() as s:
        draft, snap = _prep(s, payload={"title": "t", "owner_user_id": "U-owner"})
        draft.card_channel = "C1"
        draft.card_ts = "500.0"
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
    assert sender.posts
    assert sender.posts[0]["channel"] == "U-owner"
    assert any(b.get("type") == "actions" for b in sender.posts[0]["blocks"])


def test_finalize_without_card_coords_does_not_crash(
    patched_session_scope, SessionFactory
):
    """If the draft had no card_channel/ts (edge case, e.g. modal submit
    flow without a pre-posted widget), finalize still persists + DMs the
    owner, but skips chat.update."""
    from app.config import Settings
    from app.orchestrator.finalize import FinalizeService
    from tests.requirements.test_fr_11_12_persistence import _prep

    sender = _DualSender()
    fin = FinalizeService(settings=Settings(), sender=sender)

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
        },
    )
    assert sender.updates == []
    assert sender.posts  # DM still sent


# --------------------------------------------------------------------------- #
# Follow-up Q&A cleanup on Confirm / Ignore
# --------------------------------------------------------------------------- #


def test_confirm_cleans_up_all_followup_messages(
    patched_session_scope, SessionFactory, slack_client, finalizer_stub, ack
):
    from app.slack_bot.handlers.actions import handle_confirm
    from tests.test_persistence import _make_draft

    with SessionFactory() as s:
        d = _make_draft(s, intent=IE.create_task, payload={"title": "t"})
        d.card_channel = "C1"
        d.card_ts = "100.0"
        d.follow_up_message_ts = ["101.0", "102.0", "103.0"]
        s.commit()
        did = d.id

    sender = _DualSender()
    handle_confirm(
        body={
            "actions": [{"value": str(did)}],
            "channel": {"id": "C1"},
            "message": {"ts": "100.0", "metadata": {"event_payload": {"metadata": "{}"}}},
        },
        client=slack_client,
        services=None,
        finalizer=finalizer_stub,
        sender=sender,
        ack=ack,
    )
    deleted_ts = {d["ts"] for d in sender.deletes}
    assert deleted_ts == {"101.0", "102.0", "103.0"}


def test_confirm_empties_follow_up_list_after_cleanup(
    patched_session_scope, SessionFactory, slack_client, finalizer_stub, ack
):
    from app.slack_bot.handlers.actions import handle_confirm
    from tests.test_persistence import _make_draft

    with SessionFactory() as s:
        d = _make_draft(s, intent=IE.create_task, payload={"title": "t"})
        d.card_channel = "C1"
        d.follow_up_message_ts = ["201.0"]
        s.commit()
        did = d.id

    handle_confirm(
        body={
            "actions": [{"value": str(did)}],
            "channel": {"id": "C1"},
            "message": {"ts": "200.0", "metadata": {"event_payload": {"metadata": "{}"}}},
        },
        client=slack_client,
        services=None,
        finalizer=finalizer_stub,
        sender=_DualSender(),
        ack=ack,
    )
    with SessionFactory() as s:
        d = s.get(ActionDraft, did)
        assert d.follow_up_message_ts == []


def test_ignore_deletes_widget_and_cleans_up_followups(
    patched_session_scope, SessionFactory, ack
):
    from app.slack_bot.handlers.actions import handle_ignore
    from tests.test_persistence import _make_draft

    with SessionFactory() as s:
        d = _make_draft(s, intent=IE.create_task, payload={"title": "t"})
        d.card_channel = "C1"
        d.follow_up_message_ts = ["301.0", "302.0"]
        s.commit()
        did = d.id

    sender = _DualSender()
    handle_ignore(
        body={
            "actions": [{"value": str(did)}],
            "channel": {"id": "C1"},
            "message": {"ts": "300.0", "metadata": {"event_payload": {"metadata": "{}"}}},
        },
        ack=ack,
        sender=sender,
    )
    # Ignore kills both the widget and the follow-ups.
    deleted_ts = {d["ts"] for d in sender.deletes}
    assert "300.0" in deleted_ts  # widget
    assert "301.0" in deleted_ts
    assert "302.0" in deleted_ts


# --------------------------------------------------------------------------- #
# Transition handlers chat.update the task card
# --------------------------------------------------------------------------- #


def test_start_work_chat_updates_task_card(
    patched_session_scope, SessionFactory, ack
):
    from app.slack_bot.handlers.task_actions import handle_start_work

    with SessionFactory() as s:
        t = Task(
            title="t",
            owner_user_id="U1",
            status=TaskStatus.todo,
            card_channel="C1",
            card_ts="400.0",
            dm_channel="U1",
            dm_ts="400.5",
        )
        s.add(t)
        s.commit()
        tid = t.id

    sender = _DualSender()
    handle_start_work(
        body={
            "actions": [{"value": str(tid)}],
            "user": {"id": "U1"},
            "channel": {"id": "C1"},
        },
        sender=sender,
        ack=ack,
    )
    # Two chat.update calls: channel card + DM mirror.
    channels = {u["channel"] for u in sender.updates}
    assert channels == {"C1", "U1"}


def test_mark_done_refreshes_card(patched_session_scope, SessionFactory, ack):
    from app.slack_bot.handlers.task_actions import handle_mark_done

    with SessionFactory() as s:
        t = Task(
            title="t",
            owner_user_id="U1",
            status=TaskStatus.in_progress,
            card_channel="C1",
            card_ts="500.0",
        )
        s.add(t)
        s.commit()
        tid = t.id

    sender = _DualSender()
    handle_mark_done(
        body={
            "actions": [{"value": str(tid)}],
            "user": {"id": "U1"},
            "channel": {"id": "C1"},
        },
        sender=sender,
        ack=ack,
    )
    assert sender.updates
    assert sender.updates[0]["ts"] == "500.0"
