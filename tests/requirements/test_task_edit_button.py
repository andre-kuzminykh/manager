"""Owner (and admins) can edit a task from the task card itself via a
modal. Bystanders that click the action get an ephemeral denial and no
modal is opened.
"""
from __future__ import annotations

from datetime import date

from app.models import Task, TaskStatus
from app.slack_bot import blocks as bk
from app.slack_bot.handlers.task_actions import (
    handle_task_edit_open,
    handle_task_edit_submit,
)


class _FakeClient:
    def __init__(self):
        self.opened: list[dict] = []

    def views_open(self, *, trigger_id, view):  # noqa: N802
        self.opened.append({"trigger_id": trigger_id, "view": view})


class _Sender:
    def __init__(self):
        self.posts: list[dict] = []
        self.updates: list[dict] = []
        self.ephemerals: list[dict] = []

    def post_message(self, **kw):
        self.posts.append(kw)
        return {"ok": True, "ts": f"{len(self.posts)}.0"}

    def update_message(self, **kw):
        self.updates.append(kw)
        return {"ok": True}

    def post_ephemeral(self, **kw):
        self.ephemerals.append(kw)
        return {"ok": True}


def _make_task(session, *, owner="U-owner", status=TaskStatus.todo):
    t = Task(
        title="t",
        status=status,
        owner_user_id=owner,
        card_channel="C1",
        card_ts="100.0",
    )
    session.add(t)
    session.flush()
    return t


def _body(task_id: int, *, user: str, channel: str = "C1"):
    return {
        "actions": [{"value": str(task_id)}],
        "user": {"id": user},
        "channel": {"id": channel},
        "trigger_id": "trig-1",
    }


def test_edit_open_owner_gets_modal(patched_session_scope, SessionFactory, ack):
    with SessionFactory() as s:
        task = _make_task(s)
        s.commit()
        tid = task.id

    client = _FakeClient()
    sender = _Sender()
    handle_task_edit_open(
        body=_body(tid, user="U-owner"), client=client, sender=sender, ack=ack
    )
    assert client.opened
    view = client.opened[0]["view"]
    assert view["callback_id"] == bk.MODAL_CALLBACK_EDIT_TASK
    assert sender.ephemerals == []


def test_edit_open_bystander_gets_ephemeral_and_no_modal(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        task = _make_task(s)
        s.commit()
        tid = task.id

    client = _FakeClient()
    sender = _Sender()
    handle_task_edit_open(
        body=_body(tid, user="U-other"), client=client, sender=sender, ack=ack
    )
    assert client.opened == []
    assert sender.ephemerals
    assert sender.ephemerals[0]["user"] == "U-other"


def test_edit_submit_updates_task_and_refreshes_card(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        task = _make_task(s)
        s.commit()
        tid = task.id

    view = {
        "callback_id": bk.MODAL_CALLBACK_EDIT_TASK,
        "private_metadata": f'{{"edit_task_id": {tid}}}',
        "state": {
            "values": {
                bk.BLOCK_TITLE: {bk.INPUT_TITLE: {"value": "собрать питчдек"}},
                bk.BLOCK_DESCRIPTION: {
                    bk.INPUT_DESCRIPTION: {"value": "к понедельнику"}
                },
                bk.BLOCK_OWNER: {
                    bk.INPUT_OWNER: {
                        "selected_user": "U-owner",
                    }
                },
                bk.BLOCK_PRIORITY: {
                    bk.INPUT_PRIORITY: {
                        "selected_option": {"value": "high"}
                    }
                },
                bk.BLOCK_DUE: {bk.INPUT_DUE: {"selected_date": "2026-05-01"}},
                bk.BLOCK_EFFORT: {bk.INPUT_EFFORT: {"value": ""}},
            }
        },
    }
    sender = _Sender()
    handle_task_edit_submit(
        body={"user": {"id": "U-owner"}}, view=view, sender=sender, ack=ack
    )

    with SessionFactory() as s:
        t = s.get(Task, tid)
        assert t.title == "собрать питчдек"
        assert t.description == "к понедельнику"
        assert t.priority.value == "high"
        assert t.due_date == date(2026, 5, 1)
    # Card was refreshed in the channel.
    assert any(u["channel"] == "C1" for u in sender.updates)


def test_edit_submit_strips_owner_assumed_flag(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        task = _make_task(s)
        task.extra = {"owner_assumed": True}
        s.commit()
        tid = task.id

    view = {
        "callback_id": bk.MODAL_CALLBACK_EDIT_TASK,
        "private_metadata": f'{{"edit_task_id": {tid}}}',
        "state": {
            "values": {
                bk.BLOCK_TITLE: {bk.INPUT_TITLE: {"value": "t"}},
                bk.BLOCK_DESCRIPTION: {bk.INPUT_DESCRIPTION: {"value": ""}},
                bk.BLOCK_OWNER: {
                    bk.INPUT_OWNER: {"selected_user": "U-owner"}
                },
                bk.BLOCK_PRIORITY: {
                    bk.INPUT_PRIORITY: {
                        "selected_option": {"value": "medium"}
                    }
                },
                bk.BLOCK_DUE: {bk.INPUT_DUE: {"selected_date": None}},
                bk.BLOCK_EFFORT: {bk.INPUT_EFFORT: {"value": ""}},
            }
        },
    }
    handle_task_edit_submit(
        body={"user": {"id": "U-owner"}}, view=view, sender=_Sender(), ack=ack
    )
    with SessionFactory() as s:
        t = s.get(Task, tid)
        assert not (t.extra and t.extra.get("owner_assumed"))
