"""Requirement coverage: FR-CR-04-13 (start date/time, category,
subtasks via parent_task_id).

These three additions are pure data fields right now — no UI flow
beyond the Edit modal and no automatic pipeline population. The
calendar-booking integration that will read start_date / start_time
is intentionally deferred.
"""
from __future__ import annotations

import json
from datetime import date, time

from app.models import Task
from app.models.task import TaskPriority, TaskStatus
from app.slack_bot import blocks as bk
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


def _mk(s, **kw) -> int:
    base = dict(
        title="t",
        status=TaskStatus.todo,
        owner_user_id="U-owner",
        priority=TaskPriority.medium,
        card_channel="C1",
        card_ts="100.0",
    )
    base.update(kw)
    t = Task(**base)
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


def _edit_view(task_id: int, **fields) -> dict:
    return {
        "callback_id": bk.MODAL_CALLBACK_EDIT_TASK,
        "private_metadata": json.dumps({"edit_task_id": task_id}),
        "state": {
            "values": {
                bk.BLOCK_TITLE: {bk.INPUT_TITLE: {"value": fields.get("title", "t")}},
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
                bk.BLOCK_DUE: {bk.INPUT_DUE: {"selected_date": fields.get("due_date")}},
                bk.BLOCK_DUE_TIME: {
                    bk.INPUT_DUE_TIME: {"selected_time": fields.get("due_time")}
                },
                bk.BLOCK_START_DATE: {
                    bk.INPUT_START_DATE: {"selected_date": fields.get("start_date")}
                },
                bk.BLOCK_START_TIME: {
                    bk.INPUT_START_TIME: {"selected_time": fields.get("start_time")}
                },
                bk.BLOCK_CATEGORY: {
                    bk.INPUT_CATEGORY: {"value": fields.get("category", "")}
                },
                bk.BLOCK_EFFORT: {bk.INPUT_EFFORT: {"value": ""}},
            }
        },
    }


# --------------------------------------------------------------------------- #
# Modal: opens with all new blocks
# --------------------------------------------------------------------------- #


def test_edit_modal_includes_start_category_blocks(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        tid = _mk(s, start_date=date(2026, 5, 1), start_time=time(10, 0),
                  category="marketing")
        s.commit()

    client = _Client()
    handle_task_edit_open(
        body=_body(tid), client=client, sender=_Sender(), ack=ack
    )
    view = client.opened[0]["view"]
    bids = [b.get("block_id") for b in view["blocks"]]
    assert bk.BLOCK_START_DATE in bids
    assert bk.BLOCK_START_TIME in bids
    assert bk.BLOCK_CATEGORY in bids

    sd_block = next(b for b in view["blocks"] if b.get("block_id") == bk.BLOCK_START_DATE)
    assert sd_block["element"]["initial_date"] == "2026-05-01"
    st_block = next(b for b in view["blocks"] if b.get("block_id") == bk.BLOCK_START_TIME)
    assert st_block["element"]["initial_time"] == "10:00"
    cat_block = next(b for b in view["blocks"] if b.get("block_id") == bk.BLOCK_CATEGORY)
    assert cat_block["element"]["initial_value"] == "marketing"


# --------------------------------------------------------------------------- #
# Submit writes the new fields
# --------------------------------------------------------------------------- #


def test_edit_submit_writes_start_and_category(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        tid = _mk(s)
        s.commit()

    handle_task_edit_submit(
        body={"user": {"id": "U-owner"}},
        view=_edit_view(
            tid,
            start_date="2026-05-10",
            start_time="14:30",
            category="разработка",
        ),
        sender=_Sender(),
        ack=ack,
    )
    with SessionFactory() as s:
        t = s.get(Task, tid)
        assert t.start_date == date(2026, 5, 10)
        assert t.start_time == time(14, 30)
        assert t.category == "разработка"


def test_edit_submit_clears_start_and_category(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        tid = _mk(s, start_date=date(2026, 5, 1), start_time=time(9, 0),
                  category="ops")
        s.commit()

    handle_task_edit_submit(
        body={"user": {"id": "U-owner"}},
        view=_edit_view(tid),  # no overrides → all empty
        sender=_Sender(),
        ack=ack,
    )
    with SessionFactory() as s:
        t = s.get(Task, tid)
        assert t.start_date is None
        assert t.start_time is None
        assert t.category is None


# --------------------------------------------------------------------------- #
# Card rendering
# --------------------------------------------------------------------------- #


def test_task_card_renders_start_and_category():
    task = Task(
        id=1, title="t", status=TaskStatus.todo, owner_user_id="U-owner",
        priority=TaskPriority.medium,
        start_date=date(2026, 5, 1), start_time=time(9, 30),
        category="marketing",
    )
    flat = str(bk.task_card(task=task, viewer_slack_user_id="U-owner"))
    assert "start: 2026-05-01 09:30" in flat
    assert "category: marketing" in flat


def test_task_card_omits_start_when_no_start_date():
    task = Task(
        id=1, title="t", status=TaskStatus.todo, owner_user_id="U-owner",
        priority=TaskPriority.medium, due_date=date(2026, 5, 1),
    )
    flat = str(bk.task_card(task=task, viewer_slack_user_id="U-owner"))
    assert "start:" not in flat


# --------------------------------------------------------------------------- #
# Subtasks (parent_task_id self-reference)
# --------------------------------------------------------------------------- #


def test_task_subtasks_relationship_persists(patched_session_scope, SessionFactory):
    with SessionFactory() as s:
        parent_id = _mk(s, title="parent")
        s.commit()
        child_a = _mk(s, title="child A", parent_task_id=parent_id)
        child_b = _mk(s, title="child B", parent_task_id=parent_id)
        s.commit()

    with SessionFactory() as s:
        parent = s.get(Task, parent_id)
        assert parent.parent_task_id is None
        ids = sorted(t.id for t in parent.subtasks)
        assert ids == [child_a, child_b]
        # Each subtask points back at the parent.
        for t in parent.subtasks:
            assert t.parent.id == parent_id


def test_subtask_survives_parent_delete_with_null_parent_id(
    patched_session_scope, SessionFactory
):
    """ON DELETE SET NULL: removing a parent leaves orphaned subtasks
    with parent_task_id=null instead of cascading and losing data."""
    with SessionFactory() as s:
        pid = _mk(s, title="parent")
        cid = _mk(s, title="child", parent_task_id=pid)
        s.commit()
    with SessionFactory() as s:
        parent = s.get(Task, pid)
        s.delete(parent)
        s.commit()
    with SessionFactory() as s:
        child = s.get(Task, cid)
        assert child is not None
        assert child.parent_task_id is None
