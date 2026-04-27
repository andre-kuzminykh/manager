"""Requirement coverage: FR-CR-04-15 (recurring tasks).

Optional checkbox in the Edit modal. When ticked AND at least one
weekday is picked, the task gets is_recurring=True plus a list of
weekdays and an optional time range. When unticked the recurring
fields are cleared on submit so leftover modal state never sticks.
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


def _opt(value: str) -> dict:
    return {"value": value, "text": {"type": "plain_text", "text": value}}


def _edit_view(
    task_id: int,
    *,
    is_recurring: bool = False,
    weekdays: list[str] | None = None,
    rec_start: str | None = None,
    rec_end: str | None = None,
) -> dict:
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
                    bk.INPUT_PRIORITY: {
                        "selected_option": {"value": "medium"}
                    }
                },
                bk.BLOCK_DUE: {bk.INPUT_DUE: {"selected_date": None}},
                bk.BLOCK_DUE_TIME: {bk.INPUT_DUE_TIME: {"selected_time": None}},
                bk.BLOCK_START_DATE: {bk.INPUT_START_DATE: {"selected_date": None}},
                bk.BLOCK_START_TIME: {bk.INPUT_START_TIME: {"selected_time": None}},
                bk.BLOCK_CATEGORY: {bk.INPUT_CATEGORY: {"value": ""}},
                bk.BLOCK_RECURRING: {
                    bk.INPUT_RECURRING: {
                        "selected_options": [_opt("on")] if is_recurring else []
                    }
                },
                bk.BLOCK_RECURRING_WEEKDAYS: {
                    bk.INPUT_RECURRING_WEEKDAYS: {
                        "selected_options": [_opt(w) for w in (weekdays or [])]
                    }
                },
                bk.BLOCK_RECURRING_START: {
                    bk.INPUT_RECURRING_START: {"selected_time": rec_start}
                },
                bk.BLOCK_RECURRING_END: {
                    bk.INPUT_RECURRING_END: {"selected_time": rec_end}
                },
                bk.BLOCK_EFFORT: {bk.INPUT_EFFORT: {"value": ""}},
            }
        },
    }


# --------------------------------------------------------------------------- #
# Modal renders all four recurring blocks; populates initial values
# --------------------------------------------------------------------------- #


def test_edit_modal_includes_recurring_blocks(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        tid = _mk(
            s,
            is_recurring=True,
            recurring_weekdays=["mon", "wed"],
            recurring_start_time=time(9, 0),
            recurring_end_time=time(11, 30),
        )
        s.commit()

    client = _Client()
    handle_task_edit_open(
        body=_body(tid), client=client, sender=_Sender(), ack=ack
    )
    view = client.opened[0]["view"]
    bids = [b.get("block_id") for b in view["blocks"]]
    # No standalone "Recurring" checkbox anymore (FR-CR-04-18) —
    # picking any weekday is the toggle.
    assert bk.BLOCK_RECURRING not in bids
    for required in (
        bk.BLOCK_RECURRING_WEEKDAYS,
        bk.BLOCK_RECURRING_START,
        bk.BLOCK_RECURRING_END,
    ):
        assert required in bids

    wd_block = next(b for b in view["blocks"] if b.get("block_id") == bk.BLOCK_RECURRING_WEEKDAYS)
    initial_wds = [o["value"] for o in wd_block["element"]["initial_options"]]
    assert sorted(initial_wds) == ["mon", "wed"]

    start_block = next(
        b for b in view["blocks"] if b.get("block_id") == bk.BLOCK_RECURRING_START
    )
    assert start_block["element"]["initial_time"] == "09:00"
    end_block = next(
        b for b in view["blocks"] if b.get("block_id") == bk.BLOCK_RECURRING_END
    )
    assert end_block["element"]["initial_time"] == "11:30"


# --------------------------------------------------------------------------- #
# Submit — recurring on
# --------------------------------------------------------------------------- #


def test_submit_with_checkbox_and_weekdays_persists(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        tid = _mk(s)
        s.commit()

    handle_task_edit_submit(
        body={"user": {"id": "U-owner"}},
        view=_edit_view(
            tid,
            is_recurring=True,
            weekdays=["mon", "wed", "fri"],
            rec_start="09:00",
            rec_end="11:30",
        ),
        sender=_Sender(),
        ack=ack,
    )
    with SessionFactory() as s:
        t = s.get(Task, tid)
        assert t.is_recurring is True
        assert t.recurring_weekdays == ["mon", "wed", "fri"]
        assert t.recurring_start_time == time(9, 0)
        assert t.recurring_end_time == time(11, 30)


def test_submit_with_checkbox_but_no_weekdays_ignored(
    patched_session_scope, SessionFactory, ack
):
    """Checkbox alone isn't enough — we need at least one weekday or
    the schedule is incomplete. Treat as "not recurring"."""
    with SessionFactory() as s:
        tid = _mk(s)
        s.commit()

    handle_task_edit_submit(
        body={"user": {"id": "U-owner"}},
        view=_edit_view(tid, is_recurring=True, weekdays=[], rec_start="09:00"),
        sender=_Sender(),
        ack=ack,
    )
    with SessionFactory() as s:
        t = s.get(Task, tid)
        assert t.is_recurring is False
        assert t.recurring_weekdays is None


def test_submit_unticked_clears_existing_recurring(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        tid = _mk(
            s,
            is_recurring=True,
            recurring_weekdays=["tue"],
            recurring_start_time=time(8, 0),
            recurring_end_time=time(9, 0),
        )
        s.commit()

    handle_task_edit_submit(
        body={"user": {"id": "U-owner"}},
        view=_edit_view(tid, is_recurring=False),
        sender=_Sender(),
        ack=ack,
    )
    with SessionFactory() as s:
        t = s.get(Task, tid)
        assert t.is_recurring is False
        assert t.recurring_weekdays is None
        assert t.recurring_start_time is None
        assert t.recurring_end_time is None


# --------------------------------------------------------------------------- #
# Card render
# --------------------------------------------------------------------------- #


def test_task_card_renders_recurring_meta_with_time_range():
    task = Task(
        id=1,
        title="t",
        status=TaskStatus.todo,
        owner_user_id="U-owner",
        priority=TaskPriority.medium,
        is_recurring=True,
        recurring_weekdays=["mon", "wed"],
        recurring_start_time=time(9, 0),
        recurring_end_time=time(11, 30),
    )
    flat = str(bk.task_card(task=task, viewer_slack_user_id="U-owner"))
    assert ":repeat:" in flat
    assert "Mon/Wed" in flat
    assert "09:00" in flat and "11:30" in flat


def test_task_card_renders_recurring_without_times():
    task = Task(
        id=1,
        title="t",
        status=TaskStatus.todo,
        owner_user_id="U-owner",
        priority=TaskPriority.medium,
        is_recurring=True,
        recurring_weekdays=["fri"],
    )
    flat = str(bk.task_card(task=task, viewer_slack_user_id="U-owner"))
    assert ":repeat: Fri" in flat
    # No times → no time range section.
    assert "–" not in flat


def test_task_card_omits_repeat_when_not_recurring():
    task = Task(
        id=1,
        title="t",
        status=TaskStatus.todo,
        owner_user_id="U-owner",
        priority=TaskPriority.medium,
        is_recurring=False,
    )
    flat = str(bk.task_card(task=task, viewer_slack_user_id="U-owner"))
    assert ":repeat:" not in flat
