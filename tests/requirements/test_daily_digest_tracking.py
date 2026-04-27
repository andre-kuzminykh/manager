"""Daily digest tracking section + Manage subscriptions modal."""
from __future__ import annotations

from datetime import date, timedelta

from app.models import Task, TaskStatus, TaskSubscription
from app.services import DigestKind, DigestService, SubscriptionService
from app.slack_bot import blocks as bk
from app.slack_bot.handlers.task_actions import (
    handle_manage_subscriptions,
    handle_unsubscribe_in_modal,
)


class _Sender:
    def __init__(self):
        self.messages: list[dict] = []

    def post_message(self, **kw):
        self.messages.append(kw)
        return {"ok": True, "ts": "0"}


def _task(session, *, owner, title, status=TaskStatus.todo, due=None):
    t = Task(title=title, owner_user_id=owner, status=status, due_date=due)
    session.add(t)
    session.flush()
    return t


# --------------------------------------------------------------------------- #
# Daily digest recipient union
# --------------------------------------------------------------------------- #


def test_daily_digest_includes_subscriber_without_own_tasks(session):
    """Users who only subscribe to tasks should still receive the digest."""
    today = date(2026, 4, 20)
    owner_task = _task(session, owner="U-owner", title="t", due=today)
    SubscriptionService().subscribe(session, task=owner_task, slack_user_id="U-watcher")

    sender = _Sender()
    report = DigestService(sender=sender).send(session, DigestKind.daily, today=today)

    assert report.recipients == 2
    channels = {m["channel"] for m in sender.messages}
    assert channels == {"U-owner", "U-watcher"}


def test_daily_digest_tracking_section_lists_subscribed_tasks(session):
    today = date(2026, 4, 20)
    owner_task = _task(session, owner="U-owner", title="shared", due=today)
    SubscriptionService().subscribe(session, task=owner_task, slack_user_id="U-watcher")

    sender = _Sender()
    DigestService(sender=sender).send(session, DigestKind.daily, today=today)
    watcher_msg = next(m for m in sender.messages if m["channel"] == "U-watcher")
    body = "\n".join(
        b["text"]["text"]
        for b in watcher_msg["blocks"]
        if b.get("type") == "section"
    )
    assert "Tracking" in body
    assert "shared" in body
    assert "owner <@U-owner>" in body


def test_daily_digest_own_tasks_not_in_tracking_section(session):
    today = date(2026, 4, 20)
    mine = _task(session, owner="U-me", title="mine", due=today)
    SubscriptionService().subscribe(session, task=mine, slack_user_id="U-me")
    # I also track someone else's task
    theirs = _task(session, owner="U-other", title="theirs", due=today)
    SubscriptionService().subscribe(session, task=theirs, slack_user_id="U-me")

    sender = _Sender()
    DigestService(sender=sender).send(session, DigestKind.daily, today=today)
    mine_msg = next(m for m in sender.messages if m["channel"] == "U-me")
    sections = [
        b["text"]["text"]
        for b in mine_msg["blocks"]
        if b.get("type") == "section"
    ]
    # Tracking section mentions "theirs" but not "mine"
    tracking_section = next(s for s in sections if "Tracking" in s)
    assert "theirs" in tracking_section
    assert "mine" not in tracking_section


def test_daily_digest_has_manage_subscriptions_button(session):
    today = date(2026, 4, 20)
    _task(session, owner="U1", title="t", due=today)
    sender = _Sender()
    DigestService(sender=sender).send(session, DigestKind.daily, today=today)
    actions = [b for b in sender.messages[0]["blocks"] if b.get("type") == "actions"]
    assert actions
    ids = [el["action_id"] for b in actions for el in b["elements"]]
    assert bk.ACTION_MANAGE_SUBSCRIPTIONS in ids


def test_daily_digest_tracked_count_in_audit_log(session):
    from app.models import AuditLog

    today = date(2026, 4, 20)
    owner_task = _task(session, owner="U-owner", title="t", due=today)
    SubscriptionService().subscribe(session, task=owner_task, slack_user_id="U-watcher")
    sender = _Sender()
    DigestService(sender=sender).send(session, DigestKind.daily, today=today)
    log_for_watcher = (
        session.query(AuditLog)
        .filter(AuditLog.entity_id == "U-watcher")
        .first()
    )
    assert log_for_watcher.payload["tracked"] == 1


# --------------------------------------------------------------------------- #
# subscriptions_modal builder
# --------------------------------------------------------------------------- #


def test_subscriptions_modal_empty_state():
    view = bk.subscriptions_modal([])
    assert view["type"] == "modal"
    assert view["callback_id"] == bk.MODAL_CALLBACK_SUBSCRIPTIONS
    body_texts = [
        b["text"]["text"] for b in view["blocks"] if b.get("type") == "section"
    ]
    assert any("no active subscriptions" in t.lower() for t in body_texts)


def test_subscriptions_modal_lists_tasks_with_unsubscribe_buttons(session):
    t1 = _task(session, owner="U-other", title="first", due=date(2026, 5, 1))
    t2 = _task(session, owner="U-other", title="second")
    view = bk.subscriptions_modal([t1, t2])
    action_ids = [
        b["accessory"]["action_id"]
        for b in view["blocks"]
        if b.get("type") == "section" and "accessory" in b
    ]
    assert action_ids == [bk.ACTION_UNSUBSCRIBE_IN_MODAL, bk.ACTION_UNSUBSCRIBE_IN_MODAL]
    values = [
        b["accessory"]["value"]
        for b in view["blocks"]
        if b.get("type") == "section" and "accessory" in b
    ]
    assert values == [str(t1.id), str(t2.id)]


# --------------------------------------------------------------------------- #
# Handlers: manage_subscriptions, unsubscribe_in_modal
# --------------------------------------------------------------------------- #


class _ViewClient:
    def __init__(self):
        self.opened: list[dict] = []
        self.updated: list[dict] = []

    def views_open(self, trigger_id, view):
        self.opened.append({"trigger_id": trigger_id, "view": view})
        return {"ok": True, "view": {"id": "V1"}}

    def views_update(self, view_id, view):
        self.updated.append({"view_id": view_id, "view": view})
        return {"ok": True}


def test_manage_subscriptions_opens_modal_with_user_subs(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        t = _task(s, owner="U-owner", title="t")
        SubscriptionService().subscribe(s, task=t, slack_user_id="U-me")
        s.commit()

    client = _ViewClient()
    handle_manage_subscriptions(
        body={
            "trigger_id": "trig-1",
            "user": {"id": "U-me"},
        },
        client=client,
        ack=ack,
    )
    assert client.opened
    view = client.opened[0]["view"]
    assert view["callback_id"] == bk.MODAL_CALLBACK_SUBSCRIPTIONS


def test_manage_subscriptions_without_trigger_is_noop(
    patched_session_scope, SessionFactory, ack
):
    client = _ViewClient()
    handle_manage_subscriptions(
        body={"user": {"id": "U-me"}}, client=client, ack=ack
    )
    assert client.opened == []


def test_unsubscribe_in_modal_removes_row_and_updates_view(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        t = _task(s, owner="U-owner", title="t")
        SubscriptionService().subscribe(s, task=t, slack_user_id="U-me")
        s.commit()
        tid = t.id

    client = _ViewClient()
    handle_unsubscribe_in_modal(
        body={
            "actions": [{"value": str(tid)}],
            "user": {"id": "U-me"},
            "view": {"id": "V-current"},
        },
        client=client,
        ack=ack,
    )
    assert client.updated
    assert client.updated[0]["view_id"] == "V-current"
    with SessionFactory() as s:
        assert (
            s.query(TaskSubscription).filter_by(task_id=tid, slack_user_id="U-me").count()
            == 0
        )


def test_unsubscribe_in_modal_missing_task_id_is_noop(
    patched_session_scope, ack
):
    client = _ViewClient()
    handle_unsubscribe_in_modal(
        body={
            "actions": [{}],
            "user": {"id": "U-me"},
            "view": {"id": "V1"},
        },
        client=client,
        ack=ack,
    )
    assert client.updated == []


def test_unsubscribe_in_modal_missing_user_is_noop(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        t = _task(s, owner="U-owner", title="t")
        s.commit()
        tid = t.id
    client = _ViewClient()
    handle_unsubscribe_in_modal(
        body={
            "actions": [{"value": str(tid)}],
            "user": {},
            "view": {"id": "V1"},
        },
        client=client,
        ack=ack,
    )
    assert client.updated == []
