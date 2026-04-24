"""Every bot notification about a task should thread under the subscriber's
anchor DM, so one task = one DM thread in the bot chat."""
from __future__ import annotations

from app.models import Task, TaskStatus, TaskSubscription
from app.services import NotificationService, SubscriptionService


class _Sender:
    def __init__(self):
        self.posts: list[dict] = []

    def post_message(self, **kw):
        self.posts.append(kw)
        return {"ok": True, "ts": "anchor.1"}

    def update_message(self, **kw):
        return {"ok": True}


def test_subscribe_posts_anchor_card_and_saves_dm_ts(
    patched_session_scope, SessionFactory, ack
):
    from app.slack_bot.handlers.task_actions import handle_subscribe

    with SessionFactory() as s:
        t = Task(
            title="t",
            owner_user_id="U-owner",
            status=TaskStatus.todo,
            card_channel="C1",
            card_ts="100.0",
        )
        s.add(t)
        s.commit()
        tid = t.id

    sender = _Sender()
    handle_subscribe(
        body={
            "actions": [{"value": str(tid)}],
            "user": {"id": "U-other"},
            "channel": {"id": "C1"},
        },
        sender=sender,
        ack=ack,
        client=None,
    )
    # Anchor task card appears first in DM, ack is threaded under it.
    with SessionFactory() as s:
        sub = (
            s.query(TaskSubscription)
            .filter_by(task_id=tid, slack_user_id="U-other")
            .one()
        )
        assert sub.dm_ts == "anchor.1"


def test_broadcast_threads_under_subscriber_anchor(session):
    t = Task(title="t", owner_user_id="U1", status=TaskStatus.todo)
    session.add(t)
    session.flush()
    subs = SubscriptionService()
    s1 = subs.subscribe(session, task=t, slack_user_id="U-watch1")
    s2 = subs.subscribe(session, task=t, slack_user_id="U-watch2")
    s1.dm_ts = "anchor-1"
    s2.dm_ts = "anchor-2"
    session.flush()

    sender = _Sender()
    NotificationService(sender=sender).broadcast_status_change(
        session,
        task=t,
        from_status=TaskStatus.todo,
        to_status=TaskStatus.in_progress,
        actor_slack_user_id="U1",
    )
    by_user = {m["channel"]: m for m in sender.posts}
    assert by_user["U-watch1"]["thread_ts"] == "anchor-1"
    assert by_user["U-watch2"]["thread_ts"] == "anchor-2"


def test_broadcast_without_anchor_falls_back_to_new_dm(session):
    t = Task(title="t", owner_user_id="U1", status=TaskStatus.todo)
    session.add(t)
    session.flush()
    SubscriptionService().subscribe(session, task=t, slack_user_id="U-watch")
    # dm_ts left as None

    sender = _Sender()
    NotificationService(sender=sender).broadcast_status_change(
        session,
        task=t,
        from_status=TaskStatus.todo,
        to_status=TaskStatus.in_progress,
        actor_slack_user_id="U1",
    )
    msg = sender.posts[0]
    assert msg["channel"] == "U-watch"
    assert "thread_ts" not in msg


def test_resubscribe_does_not_post_duplicate_anchor(
    patched_session_scope, SessionFactory, ack
):
    from app.slack_bot.handlers.task_actions import handle_subscribe

    with SessionFactory() as s:
        t = Task(
            title="t",
            owner_user_id="U-owner",
            status=TaskStatus.todo,
            card_channel="C1",
            card_ts="100.0",
        )
        s.add(t)
        s.commit()
        tid = t.id

    sender = _Sender()
    body = {
        "actions": [{"value": str(tid)}],
        "user": {"id": "U-other"},
        "channel": {"id": "C1"},
    }
    handle_subscribe(body=body, sender=sender, ack=ack, client=None)
    first_dm_posts = sum(1 for m in sender.posts if m["channel"] == "U-other")

    # Click Subscribe again (edge case — button already said Unsubscribe
    # but simulate rapid double-click). Anchor already set, so no new
    # anchor must be posted.
    sender.posts.clear()
    handle_subscribe(body=body, sender=sender, ack=ack, client=None)
    second_dm_posts = [m for m in sender.posts if m["channel"] == "U-other"]
    # No new anchor card. Anchor = top-level DM (no thread_ts); the
    # ack is a thread reply under the existing anchor, so it has
    # thread_ts set. Acks also carry the task-card preview as blocks
    # now, so we can't distinguish by "blocks in m" anymore — check
    # thread_ts instead.
    anchors = [m for m in second_dm_posts if not m.get("thread_ts")]
    assert anchors == []


def test_finalize_attaches_dm_ts_to_owner_subscription(
    patched_session_scope, SessionFactory
):
    """Creation DM anchors the owner's thread too."""
    from app.config import Settings
    from app.orchestrator.finalize import FinalizeService
    from tests.requirements.test_fr_11_12_persistence import _prep

    class _FinalSender:
        def __init__(self):
            self.posts = []

        def post_message(self, **kw):
            self.posts.append(kw)
            return {"ok": True, "ts": "owner-anchor.0"}

        def update_message(self, **kw):
            return {"ok": True}

    sender = _FinalSender()
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
        sub = (
            s.query(TaskSubscription)
            .filter_by(slack_user_id="U-owner")
            .one()
        )
        assert sub.dm_ts == "owner-anchor.0"


def test_owner_status_change_threads_under_own_dm(
    patched_session_scope, SessionFactory
):
    from app.config import Settings
    from app.orchestrator.finalize import FinalizeService
    from tests.requirements.test_fr_11_12_persistence import _prep

    class _S:
        def __init__(self):
            self.posts = []

        def post_message(self, **kw):
            self.posts.append(kw)
            return {"ok": True, "ts": "anchor.owner"}

        def update_message(self, **kw):
            return {"ok": True}

    sender = _S()
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
    # Now simulate a status change broadcast.
    with SessionFactory() as s:
        t = s.query(Task).one()
        sender.posts.clear()
        NotificationService(sender=sender).broadcast_status_change(
            s,
            task=t,
            from_status=TaskStatus.backlog,
            to_status=TaskStatus.in_progress,
            actor_slack_user_id="U-owner",
        )
    owner_msg = next(m for m in sender.posts if m["channel"] == "U-owner")
    assert owner_msg["thread_ts"] == "anchor.owner"
