"""Optional clock time on a task — filled via the Edit modal,
displayed alongside due_date on the task card."""
from __future__ import annotations

import json
from datetime import date, time

from app.models import AuditLog, Task
from app.models.task import TaskPriority, TaskStatus
from app.slack_bot import blocks as bk
from app.slack_bot.handlers.admin_review import (
    handle_admin_edit_open,
    handle_admin_edit_submit,
)
from app.slack_bot.handlers.task_actions import (
    handle_task_edit_open,
    handle_task_edit_submit,
)


class _Sender:
    def __init__(self):
        self.posts = []
        self.updates = []
        self.ephemerals = []

    def post_message(self, **kw):
        self.posts.append(kw)
        return {"ok": True, "ts": "1.0"}

    def update_message(self, **kw):
        self.updates.append(kw)
        return {"ok": True}

    def post_ephemeral(self, **kw):
        self.ephemerals.append(kw)
        return {"ok": True}


class _Client:
    def __init__(self):
        self.opened: list[dict] = []

    def views_open(self, *, trigger_id, view):  # noqa: N802
        self.opened.append({"trigger_id": trigger_id, "view": view})


def _mk_task(s, *, owner="U-owner", due=date(2026, 5, 1), tt=None) -> int:
    t = Task(
        title="t",
        status=TaskStatus.todo,
        owner_user_id=owner,
        priority=TaskPriority.medium,
        due_date=due,
        due_time=tt,
        card_channel="C1",
        card_ts="100.0",
    )
    s.add(t)
    s.flush()
    return t.id


def _body(task_id: int, *, user="U-owner"):
    return {
        "actions": [{"value": str(task_id)}],
        "user": {"id": user},
        "channel": {"id": "C1"},
        "message": {"ts": "100.0"},
        "trigger_id": "trig-1",
    }


def _edit_view(task_id: int, *, due_date_str=None, due_time_str=None) -> dict:
    return {
        "callback_id": bk.MODAL_CALLBACK_EDIT_TASK,
        "private_metadata": json.dumps({"edit_task_id": task_id}),
        "state": {
            "values": {
                bk.BLOCK_TITLE: {bk.INPUT_TITLE: {"value": "t"}},
                bk.BLOCK_DESCRIPTION: {bk.INPUT_DESCRIPTION: {"value": ""}},
                bk.BLOCK_OWNER: {
                    bk.INPUT_OWNER: {
                        "selected_option": {
                            "value": "U-owner",
                            "text": {"type": "plain_text", "text": "Owner"},
                        }
                    }
                },
                bk.BLOCK_PRIORITY: {
                    bk.INPUT_PRIORITY: {"selected_option": {"value": "medium"}}
                },
                bk.BLOCK_DUE: {bk.INPUT_DUE: {"selected_date": due_date_str}},
                bk.BLOCK_DUE_TIME: {
                    bk.INPUT_DUE_TIME: {"selected_time": due_time_str}
                },
                bk.BLOCK_EFFORT: {bk.INPUT_EFFORT: {"value": ""}},
            }
        },
    }


# --------------------------------------------------------------------------- #
# Modal carries due_time — open + submit
# --------------------------------------------------------------------------- #


def test_edit_modal_includes_timepicker_block(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        tid = _mk_task(s, tt=time(15, 30))
        s.commit()

    client = _Client()
    sender = _Sender()
    handle_task_edit_open(
        body=_body(tid), client=client, sender=sender, ack=ack
    )
    view = client.opened[0]["view"]
    block_ids = [b.get("block_id") for b in view["blocks"]]
    assert bk.BLOCK_DUE_TIME in block_ids
    # Initial value is the existing time on the task.
    time_block = next(b for b in view["blocks"] if b.get("block_id") == bk.BLOCK_DUE_TIME)
    assert time_block["element"]["initial_time"] == "15:30"


def test_edit_submit_writes_due_time(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        tid = _mk_task(s)
        s.commit()

    handle_task_edit_submit(
        body={"user": {"id": "U-owner"}},
        view=_edit_view(tid, due_date_str="2026-05-01", due_time_str="14:30"),
        sender=_Sender(),
        ack=ack,
    )
    with SessionFactory() as s:
        t = s.get(Task, tid)
        assert t.due_time == time(14, 30)
        assert t.due_date == date(2026, 5, 1)


def test_edit_submit_clears_due_time_when_unset(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        tid = _mk_task(s, tt=time(9, 0))
        s.commit()

    handle_task_edit_submit(
        body={"user": {"id": "U-owner"}},
        view=_edit_view(tid, due_date_str="2026-05-01", due_time_str=None),
        sender=_Sender(),
        ack=ack,
    )
    with SessionFactory() as s:
        t = s.get(Task, tid)
        assert t.due_time is None


def test_admin_edit_records_due_time_diff(
    patched_session_scope, SessionFactory, ack, monkeypatch
):
    monkeypatch.setenv("ADMIN_SLACK_USER_IDS", "U-admin")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        with SessionFactory() as s:
            tid = _mk_task(s, tt=None)
            s.commit()

        view = {
            "callback_id": bk.MODAL_CALLBACK_ADMIN_EDIT,
            "private_metadata": json.dumps({"edit_task_id": tid}),
            "state": {
                "values": {
                    bk.BLOCK_TITLE: {bk.INPUT_TITLE: {"value": "t"}},
                    bk.BLOCK_DESCRIPTION: {bk.INPUT_DESCRIPTION: {"value": ""}},
                    bk.BLOCK_OWNER: {
                        bk.INPUT_OWNER: {
                            "selected_option": {
                                "value": "U-owner",
                                "text": {"type": "plain_text", "text": "Owner"},
                            }
                        }
                    },
                    bk.BLOCK_PRIORITY: {
                        bk.INPUT_PRIORITY: {"selected_option": {"value": "medium"}}
                    },
                    bk.BLOCK_DUE: {bk.INPUT_DUE: {"selected_date": "2026-05-01"}},
                    bk.BLOCK_DUE_TIME: {
                        bk.INPUT_DUE_TIME: {"selected_time": "10:00"}
                    },
                    bk.BLOCK_EFFORT: {bk.INPUT_EFFORT: {"value": ""}},
                }
            },
        }
        handle_admin_edit_submit(
            body={"user": {"id": "U-admin"}}, view=view, sender=_Sender(), ack=ack
        )
        with SessionFactory() as s:
            t = s.get(Task, tid)
            assert t.due_time == time(10, 0)
            audit = (
                s.query(AuditLog)
                .filter(AuditLog.category == "admin_review")
                .order_by(AuditLog.id.desc())
                .first()
            )
            assert "due_time" in (audit.payload or {}).get("diff", {})
            assert audit.payload["diff"]["due_time"] == [None, "10:00"]
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Card rendering
# --------------------------------------------------------------------------- #


def test_task_card_renders_due_time_alongside_date():
    task = Task(
        id=1, title="t", status=TaskStatus.todo, owner_user_id="U-owner",
        priority=TaskPriority.medium,
        due_date=date(2026, 5, 1), due_time=time(14, 30),
    )
    blocks = bk.task_card(task=task, viewer_slack_user_id="U-owner")
    flat = str(blocks)
    assert "2026-05-01 14:30" in flat


def test_task_card_omits_time_when_due_time_null():
    task = Task(
        id=1, title="t", status=TaskStatus.todo, owner_user_id="U-owner",
        priority=TaskPriority.medium,
        due_date=date(2026, 5, 1), due_time=None,
    )
    blocks = bk.task_card(task=task, viewer_slack_user_id="U-owner")
    flat = str(blocks)
    assert "due: 2026-05-01" in flat
    assert "14:30" not in flat
