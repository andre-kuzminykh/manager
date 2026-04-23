"""Subscribe / Unsubscribe must:
- update the channel widget AND the DM mirror in place via chat.update,
- flip the button label (Подписаться ↔ Отписаться) and add a :star: in
  the title for subscribed viewers,
- DM the actor a hyperlink to the channel task card."""
from __future__ import annotations

from types import SimpleNamespace

from app.models import Task, TaskStatus
from app.slack_bot import blocks as bk
from app.slack_bot.handlers.task_actions import handle_subscribe, handle_unsubscribe


# --------------------------------------------------------------------------- #
# task_card star rendering
# --------------------------------------------------------------------------- #


def test_task_card_shows_star_when_subscribed(session):
    t = Task(title="t", owner_user_id="U-owner", status=TaskStatus.todo)
    session.add(t)
    session.flush()
    blocks = bk.task_card(task=t, viewer_slack_user_id="U-other", is_subscribed=True)
    title_text = blocks[0]["text"]["text"]
    assert ":star:" in title_text
    meta_text = blocks[1]["elements"][0]["text"]
    assert "subscribed" in meta_text


def test_task_card_no_star_when_not_subscribed(session):
    t = Task(title="t", owner_user_id="U-owner", status=TaskStatus.todo)
    session.add(t)
    session.flush()
    blocks = bk.task_card(task=t, viewer_slack_user_id="U-other", is_subscribed=False)
    title_text = blocks[0]["text"]["text"]
    assert ":star:" not in title_text


def test_task_card_subscribed_button_is_unsubscribe(session):
    t = Task(title="t", owner_user_id="U-owner", status=TaskStatus.todo)
    session.add(t)
    session.flush()
    blocks = bk.task_card(task=t, viewer_slack_user_id="U-other", is_subscribed=True)
    ids = [
        el["action_id"]
        for b in blocks
        if b["type"] == "actions"
        for el in b["elements"]
    ]
    assert bk.ACTION_UNSUBSCRIBE in ids
    assert bk.ACTION_SUBSCRIBE not in ids


def test_task_card_not_subscribed_button_is_subscribe(session):
    t = Task(title="t", owner_user_id="U-owner", status=TaskStatus.todo)
    session.add(t)
    session.flush()
    blocks = bk.task_card(task=t, viewer_slack_user_id="U-other", is_subscribed=False)
    ids = [
        el["action_id"]
        for b in blocks
        if b["type"] == "actions"
        for el in b["elements"]
    ]
    assert bk.ACTION_SUBSCRIBE in ids
    assert bk.ACTION_UNSUBSCRIBE not in ids


# --------------------------------------------------------------------------- #
# Subscribe / Unsubscribe handlers — chat.update + DM ack with link
# --------------------------------------------------------------------------- #


class _FakeClient:
    def __init__(self, permalink="https://x/permalink"):
        self.permalink = permalink

    def chat_getPermalink(self, channel, message_ts):  # noqa: N802
        return {"permalink": self.permalink}


class _DualSender:
    def __init__(self):
        self.posts: list[dict] = []
        self.updates: list[dict] = []

    def post_message(self, **kw):
        self.posts.append(kw)
        return {"ok": True, "ts": "1.0"}

    def update_message(self, **kw):
        self.updates.append(kw)
        return {"ok": True}


def test_subscribe_updates_widget_in_place(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        t = Task(
            title="t",
            owner_user_id="U-owner",
            status=TaskStatus.todo,
            card_channel="C1",
            card_ts="100.0",
            dm_channel="U-owner",
            dm_ts="100.5",
        )
        s.add(t)
        s.commit()
        tid = t.id

    sender = _DualSender()
    handle_subscribe(
        body={
            "actions": [{"value": str(tid)}],
            "user": {"id": "U-other"},
            "channel": {"id": "C1"},
        },
        sender=sender,
        ack=ack,
        client=_FakeClient(),
    )
    # Channel card was chat.update'd
    chans = {u["channel"] for u in sender.updates}
    assert "C1" in chans
    assert "U-owner" in chans  # DM mirror also refreshed


def test_subscribe_dms_actor_with_clickable_link(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        t = Task(
            title="t",
            owner_user_id="U-owner",
            status=TaskStatus.todo,
            card_channel="C1",
            card_ts="200.0",
        )
        s.add(t)
        s.commit()
        tid = t.id

    sender = _DualSender()
    handle_subscribe(
        body={
            "actions": [{"value": str(tid)}],
            "user": {"id": "U-other"},
            "channel": {"id": "C1"},
        },
        sender=sender,
        ack=ack,
        client=_FakeClient(permalink="https://slack.com/x/p200"),
    )
    # DM ack went to the actor (channel == actor user id).
    actor_msgs = [m for m in sender.posts if m["channel"] == "U-other"]
    assert actor_msgs
    txt = actor_msgs[0]["text"]
    assert ":bell:" in txt
    assert "subscribed" in txt
    assert "<https://slack.com/x/p200|task #" in txt


def test_subscribe_button_flips_to_unsubscribe_on_re_render(
    patched_session_scope, SessionFactory, ack
):
    """After Subscribe the chat.update payload must contain the
    Отписаться button (ACTION_UNSUBSCRIBE) for the actor's view."""
    with SessionFactory() as s:
        t = Task(
            title="t",
            owner_user_id="U-owner",
            status=TaskStatus.todo,
            card_channel="C1",
            card_ts="300.0",
        )
        s.add(t)
        s.commit()
        tid = t.id

    sender = _DualSender()
    handle_subscribe(
        body={
            "actions": [{"value": str(tid)}],
            "user": {"id": "U-other"},
            "channel": {"id": "C1"},
        },
        sender=sender,
        ack=ack,
        client=_FakeClient(),
    )
    chan_update = next(u for u in sender.updates if u["channel"] == "C1")
    ids = [
        el["action_id"]
        for b in chan_update["blocks"]
        if b["type"] == "actions"
        for el in b["elements"]
    ]
    assert bk.ACTION_UNSUBSCRIBE in ids


def test_unsubscribe_button_flips_back_to_subscribe(
    patched_session_scope, SessionFactory, ack
):
    from app.services import SubscriptionService

    with SessionFactory() as s:
        t = Task(
            title="t",
            owner_user_id="U-owner",
            status=TaskStatus.todo,
            card_channel="C1",
            card_ts="400.0",
        )
        s.add(t)
        s.commit()
        SubscriptionService().subscribe(s, task=t, slack_user_id="U-other")
        s.commit()
        tid = t.id

    sender = _DualSender()
    handle_unsubscribe(
        body={
            "actions": [{"value": str(tid)}],
            "user": {"id": "U-other"},
            "channel": {"id": "C1"},
        },
        sender=sender,
        ack=ack,
        client=_FakeClient(),
    )
    chan_update = next(u for u in sender.updates if u["channel"] == "C1")
    ids = [
        el["action_id"]
        for b in chan_update["blocks"]
        if b["type"] == "actions"
        for el in b["elements"]
    ]
    assert bk.ACTION_SUBSCRIBE in ids


def test_subscribe_chat_update_includes_star_for_actor(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        t = Task(
            title="t",
            owner_user_id="U-owner",
            status=TaskStatus.todo,
            card_channel="C1",
            card_ts="500.0",
        )
        s.add(t)
        s.commit()
        tid = t.id

    sender = _DualSender()
    handle_subscribe(
        body={
            "actions": [{"value": str(tid)}],
            "user": {"id": "U-other"},
            "channel": {"id": "C1"},
        },
        sender=sender,
        ack=ack,
        client=_FakeClient(),
    )
    chan_update = next(u for u in sender.updates if u["channel"] == "C1")
    title = chan_update["blocks"][0]["text"]["text"]
    assert ":star:" in title


def test_subscribe_without_card_coords_still_dms_user(
    patched_session_scope, SessionFactory, ack
):
    """If task has no card coordinates, chat.update is skipped but the DM
    ack still goes through."""
    with SessionFactory() as s:
        t = Task(title="t", owner_user_id="U-owner", status=TaskStatus.todo)
        s.add(t)
        s.commit()
        tid = t.id

    sender = _DualSender()
    handle_subscribe(
        body={
            "actions": [{"value": str(tid)}],
            "user": {"id": "U-other"},
            "channel": {"id": "C1"},
        },
        sender=sender,
        ack=ack,
        client=_FakeClient(),
    )
    assert sender.updates == []
    actor_msgs = [m for m in sender.posts if m["channel"] == "U-other"]
    assert actor_msgs


def test_subscribe_swallows_card_refresh_failure(
    patched_session_scope, SessionFactory, ack
):
    """A failing chat.update must not abort the subscription record."""
    from app.services import SubscriptionService

    class BrokenSender:
        def __init__(self):
            self.posts = []

        def post_message(self, **kw):
            self.posts.append(kw)
            return {"ok": True, "ts": "0"}

        def update_message(self, **kw):
            raise RuntimeError("nope")

    with SessionFactory() as s:
        t = Task(
            title="t",
            owner_user_id="U-owner",
            status=TaskStatus.todo,
            card_channel="C1",
            card_ts="600.0",
        )
        s.add(t)
        s.commit()
        tid = t.id

    sender = BrokenSender()
    handle_subscribe(
        body={
            "actions": [{"value": str(tid)}],
            "user": {"id": "U-other"},
            "channel": {"id": "C1"},
        },
        sender=sender,
        ack=ack,
        client=_FakeClient(),
    )
    with SessionFactory() as s:
        from app.services import SubscriptionService

        assert SubscriptionService().is_subscribed(
            s,
            task=s.get(Task, tid),
            slack_user_id="U-other",
        )
