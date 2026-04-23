"""Tests for CR-01 task-card handlers (FR-CR-4 start_work, submit_review,
mark_done, subscribe, unsubscribe, show_context)."""
from __future__ import annotations

from datetime import date

import pytest

from app.models import ContextSnapshot, Task, TaskStatus, TaskStatusHistory, TaskSubscription
from app.slack_bot.handlers.task_actions import (
    handle_mark_done,
    handle_open_source,
    handle_show_context,
    handle_start_work,
    handle_submit_review,
    handle_subscribe,
    handle_unsubscribe,
)


def _make_task(
    session, *, status=TaskStatus.todo, owner="U-owner", permalink=None, snapshot_id=None
):
    t = Task(
        title="demo",
        status=status,
        owner_user_id=owner,
        source_permalink=permalink,
        context_snapshot_id=snapshot_id,
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


# --------------------------------------------------------------------------- #
# Start work
# --------------------------------------------------------------------------- #


def test_start_work_transitions_task(patched_session_scope, SessionFactory, sender, ack):
    with SessionFactory() as s:
        task = _make_task(s, status=TaskStatus.todo, owner="U1")
        s.commit()
        tid = task.id

    handle_start_work(body=_body(tid, user="U1"), sender=sender, ack=ack)

    with SessionFactory() as s:
        assert s.get(Task, tid).status == TaskStatus.in_progress


def test_start_work_records_history(patched_session_scope, SessionFactory, sender, ack):
    with SessionFactory() as s:
        task = _make_task(s, status=TaskStatus.todo, owner="U1")
        s.commit()
        tid = task.id

    handle_start_work(body=_body(tid, user="U1"), sender=sender, ack=ack)

    with SessionFactory() as s:
        hist = (
            s.query(TaskStatusHistory)
            .filter_by(task_id=tid)
            .order_by(TaskStatusHistory.id.desc())
            .first()
        )
        assert hist.to_status == TaskStatus.in_progress
        assert hist.changed_by_slack_user_id == "U1"


def test_start_work_non_owner_rejected(patched_session_scope, SessionFactory, sender, ack):
    with SessionFactory() as s:
        task = _make_task(s, status=TaskStatus.todo, owner="U-owner")
        s.commit()
        tid = task.id

    handle_start_work(body=_body(tid, user="U-stranger"), sender=sender, ack=ack)

    with SessionFactory() as s:
        assert s.get(Task, tid).status == TaskStatus.todo
    # The handler must notify the channel
    assert any("Only <@U-owner>" in m.get("text", "") for m in sender.posted)


def test_start_work_from_backlog_allowed(patched_session_scope, SessionFactory, sender, ack):
    with SessionFactory() as s:
        task = _make_task(s, status=TaskStatus.backlog, owner="U1")
        s.commit()
        tid = task.id
    handle_start_work(body=_body(tid, user="U1"), sender=sender, ack=ack)
    with SessionFactory() as s:
        assert s.get(Task, tid).status == TaskStatus.in_progress


def test_start_work_sets_started_at(patched_session_scope, SessionFactory, sender, ack):
    with SessionFactory() as s:
        task = _make_task(s, status=TaskStatus.todo, owner="U1")
        s.commit()
        tid = task.id
    handle_start_work(body=_body(tid, user="U1"), sender=sender, ack=ack)
    with SessionFactory() as s:
        assert s.get(Task, tid).started_at is not None


def test_start_work_unknown_task_is_noop(patched_session_scope, SessionFactory, sender, ack):
    handle_start_work(body=_body(999, user="U1"), sender=sender, ack=ack)
    assert sender.posted == []


def test_start_work_missing_value_is_noop(patched_session_scope, sender, ack):
    handle_start_work(
        body={"actions": [{}], "user": {"id": "U1"}, "channel": {"id": "C1"}},
        sender=sender,
        ack=ack,
    )
    assert ack.called


def test_start_work_invalid_transition_sends_warning(
    patched_session_scope, SessionFactory, sender, ack
):
    with SessionFactory() as s:
        task = _make_task(s, status=TaskStatus.in_progress, owner="U1")
        s.commit()
        tid = task.id
    handle_start_work(body=_body(tid, user="U1"), sender=sender, ack=ack)
    assert any(
        "cannot transition" in m.get("text", "") or "already in" in m.get("text", "")
        for m in sender.posted
    )


# --------------------------------------------------------------------------- #
# Submit review / mark done
# --------------------------------------------------------------------------- #


def test_submit_review_moves_to_review(patched_session_scope, SessionFactory, sender, ack):
    with SessionFactory() as s:
        task = _make_task(s, status=TaskStatus.in_progress, owner="U1")
        s.commit()
        tid = task.id
    handle_submit_review(body=_body(tid, user="U1"), sender=sender, ack=ack)
    with SessionFactory() as s:
        assert s.get(Task, tid).status == TaskStatus.review


def test_mark_done_from_review_closes_task(
    patched_session_scope, SessionFactory, sender, ack
):
    with SessionFactory() as s:
        task = _make_task(s, status=TaskStatus.review, owner="U1")
        s.commit()
        tid = task.id
    handle_mark_done(body=_body(tid, user="U1"), sender=sender, ack=ack)
    with SessionFactory() as s:
        t = s.get(Task, tid)
        assert t.status == TaskStatus.done
        assert t.completed_at is not None


def test_mark_done_from_backlog_allowed(patched_session_scope, SessionFactory, sender, ack):
    with SessionFactory() as s:
        task = _make_task(s, status=TaskStatus.backlog, owner="U1")
        s.commit()
        tid = task.id
    handle_mark_done(body=_body(tid, user="U1"), sender=sender, ack=ack)
    with SessionFactory() as s:
        assert s.get(Task, tid).status == TaskStatus.done


# --------------------------------------------------------------------------- #
# Subscribe / unsubscribe buttons
# --------------------------------------------------------------------------- #


def test_subscribe_button_creates_subscription(
    patched_session_scope, SessionFactory, sender, ack
):
    with SessionFactory() as s:
        task = _make_task(s)
        s.commit()
        tid = task.id
    handle_subscribe(body=_body(tid, user="U-new"), sender=sender, ack=ack)
    with SessionFactory() as s:
        assert (
            s.query(TaskSubscription).filter_by(task_id=tid, slack_user_id="U-new").count() == 1
        )


def test_subscribe_button_is_idempotent(
    patched_session_scope, SessionFactory, sender, ack
):
    with SessionFactory() as s:
        task = _make_task(s)
        s.commit()
        tid = task.id
    handle_subscribe(body=_body(tid, user="U-new"), sender=sender, ack=ack)
    handle_subscribe(body=_body(tid, user="U-new"), sender=sender, ack=ack)
    with SessionFactory() as s:
        assert s.query(TaskSubscription).count() == 1


def test_unsubscribe_removes_subscription(
    patched_session_scope, SessionFactory, sender, ack
):
    with SessionFactory() as s:
        task = _make_task(s)
        s.commit()
        tid = task.id
    handle_subscribe(body=_body(tid, user="U-x"), sender=sender, ack=ack)
    handle_unsubscribe(body=_body(tid, user="U-x"), sender=sender, ack=ack)
    with SessionFactory() as s:
        assert s.query(TaskSubscription).count() == 0


def test_subscribe_missing_user_is_noop(patched_session_scope, sender, ack):
    handle_subscribe(
        body={"actions": [{"value": "1"}], "user": {}, "channel": {"id": "C1"}},
        sender=sender,
        ack=ack,
    )
    assert ack.called
    assert sender.posted == []


def test_unsubscribe_missing_user_is_noop(patched_session_scope, sender, ack):
    handle_unsubscribe(
        body={"actions": [{"value": "1"}], "user": {}, "channel": {"id": "C1"}},
        sender=sender,
        ack=ack,
    )
    assert ack.called
    assert sender.posted == []


# --------------------------------------------------------------------------- #
# Open source / show context
# --------------------------------------------------------------------------- #


def test_open_source_is_noop_but_acks(ack):
    handle_open_source(body={"actions": [{"value": "1"}]}, ack=ack)
    assert ack.called


def test_show_context_opens_modal_with_snapshot(
    patched_session_scope, SessionFactory, slack_client, ack
):
    with SessionFactory() as s:
        snap = ContextSnapshot(
            conversation_id="C1",
            source_ts="2.0",
            source_message={"ts": "2.0", "text": "source msg", "user": "U1"},
            history_before=[{"ts": "1.0", "text": "earlier", "user": "U2"}],
            thread_messages=[],
        )
        s.add(snap)
        s.commit()
        sid = snap.id

    handle_show_context(
        body={
            "actions": [{"value": str(sid)}],
            "trigger_id": "trg",
        },
        client=slack_client,
        ack=ack,
    )
    assert slack_client.views_opened
    blocks_text = "".join(
        b["text"]["text"]
        for b in slack_client.views_opened[0]["view"]["blocks"]
        if b.get("type") == "section"
    )
    assert "source msg" in blocks_text
    assert "earlier" in blocks_text


def test_show_context_without_trigger_id_noop(patched_session_scope, slack_client, ack):
    handle_show_context(
        body={"actions": [{"value": "1"}]}, client=slack_client, ack=ack
    )
    assert slack_client.views_opened == []


def test_show_context_unknown_snapshot_noop(
    patched_session_scope, slack_client, ack
):
    handle_show_context(
        body={"actions": [{"value": "99999"}], "trigger_id": "t"},
        client=slack_client,
        ack=ack,
    )
    assert slack_client.views_opened == []
