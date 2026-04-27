"""Requirement coverage: FR-CR-04-14 (daily plan workflow).

  Evening (18:00 London)  → plan-evening
    DM with one Skip button per candidate task + Approve button.
    Tracking section: tasks the user subscribes to (favourites).
  Morning (09:00 next day) → plan-morning
    DM with the surviving task cards + Tracking section.
  Idempotency: re-running the same phase on the same day for the
    same user is a no-op (audit_logs key).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.models import (
    AuditLog,
    DailyPlanItem,
    Task,
    TaskStatus,
    TaskSubscription,
)
from app.models.task import TaskPriority
from app.services.daily_plan import (
    send_evening_plan,
    send_morning_plan,
    skip_plan_item,
)
from app.slack_bot import blocks as bk
from app.slack_bot.handlers.daily_plan import (
    handle_plan_approve,
    handle_plan_skip,
)


class _Sender:
    def __init__(self):
        self.posts = []

    def post_message(self, **kw):
        self.posts.append(kw)
        return {"ok": True, "ts": "100.0"}


def _mk_task(s, **kw) -> Task:
    base = dict(
        title="t",
        status=TaskStatus.todo,
        owner_user_id="U-owner",
        priority=TaskPriority.medium,
    )
    base.update(kw)
    t = Task(**base)
    s.add(t)
    s.flush()
    return t


# --------------------------------------------------------------------------- #
# Evening: candidate selection
# --------------------------------------------------------------------------- #


def test_evening_picks_tasks_due_tomorrow(patched_session_scope, SessionFactory):
    plan_date = date(2026, 5, 1)
    with SessionFactory() as s:
        t_due = _mk_task(s, title="due", due_date=plan_date)
        # is_current_week defaults to True; clear it so this task only
        # qualifies via due_date (which we set far away).
        t_other = _mk_task(
            s,
            title="far",
            due_date=plan_date + timedelta(days=10),
            is_current_week=False,
        )
        t_done = _mk_task(s, title="done", due_date=plan_date, status=TaskStatus.done)
        s.commit()
        sender = _Sender()
        report = send_evening_plan(
            s, sender=sender, plan_date=plan_date, user_ids=["U-owner"]
        )
        s.commit()

    assert report.sent == 1
    with SessionFactory() as s:
        items = (
            s.query(DailyPlanItem)
            .filter(DailyPlanItem.user_id == "U-owner")
            .all()
        )
        ids = sorted(i.task_id for i in items)
        # t_due included; t_other skipped (too far); t_done excluded
        # (status filter).
        assert ids == [t_due.id]


def test_evening_picks_tasks_starting_tomorrow(patched_session_scope, SessionFactory):
    plan_date = date(2026, 5, 1)
    with SessionFactory() as s:
        t = _mk_task(s, start_date=plan_date)
        s.commit()
        send_evening_plan(
            s, sender=_Sender(), plan_date=plan_date, user_ids=["U-owner"]
        )
        s.commit()
    with SessionFactory() as s:
        items = s.query(DailyPlanItem).all()
        assert len(items) == 1
        assert items[0].task_id == t.id


def test_evening_includes_current_week_todo_tasks(
    patched_session_scope, SessionFactory
):
    plan_date = date(2026, 5, 1)
    with SessionFactory() as s:
        t = _mk_task(s, is_current_week=True, status=TaskStatus.todo)
        s.commit()
        send_evening_plan(
            s, sender=_Sender(), plan_date=plan_date, user_ids=["U-owner"]
        )
        s.commit()
    with SessionFactory() as s:
        items = s.query(DailyPlanItem).all()
        assert len(items) == 1
        assert items[0].task_id == t.id


def test_evening_omits_other_users_tasks(patched_session_scope, SessionFactory):
    plan_date = date(2026, 5, 1)
    with SessionFactory() as s:
        _mk_task(s, owner_user_id="U-other", due_date=plan_date)
        s.commit()
        report = send_evening_plan(
            s, sender=_Sender(), plan_date=plan_date, user_ids=["U-owner"]
        )
        s.commit()
    assert report.sent == 0
    assert report.no_tasks == 1


# --------------------------------------------------------------------------- #
# Evening: DM contents
# --------------------------------------------------------------------------- #


def test_evening_dm_contains_skip_and_approve_buttons(
    patched_session_scope, SessionFactory
):
    plan_date = date(2026, 5, 1)
    with SessionFactory() as s:
        t = _mk_task(s, due_date=plan_date)
        s.commit()
        sender = _Sender()
        send_evening_plan(s, sender=sender, plan_date=plan_date, user_ids=["U-owner"])
        s.commit()

    assert sender.posts
    blocks = sender.posts[0]["blocks"]
    flat = str(blocks)
    assert bk.ACTION_PLAN_SKIP in flat
    assert bk.ACTION_PLAN_APPROVE in flat
    # Skip value carries plan_date|task_id.
    skip_btn = next(
        el
        for b in blocks
        if b.get("type") == "section"
        for el in [b.get("accessory")]
        if el and el.get("action_id") == bk.ACTION_PLAN_SKIP
    )
    assert skip_btn["value"] == f"{plan_date.isoformat()}|{t.id}"


def test_evening_dm_lists_tracking_subscriptions(
    patched_session_scope, SessionFactory
):
    plan_date = date(2026, 5, 1)
    with SessionFactory() as s:
        own = _mk_task(s, due_date=plan_date)
        other = _mk_task(s, owner_user_id="U-other", title="watched")
        s.add(TaskSubscription(task_id=other.id, slack_user_id="U-owner"))
        s.commit()
        sender = _Sender()
        send_evening_plan(
            s, sender=sender, plan_date=plan_date, user_ids=["U-owner"]
        )
        s.commit()

    flat = str(sender.posts[0]["blocks"])
    # The subscribed task shows up in the Tracking section even
    # though the user doesn't own it.
    assert "Отслеживаемые" in flat
    assert f"#{other.id}" in flat


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #


def test_evening_is_idempotent_per_user_per_day(
    patched_session_scope, SessionFactory
):
    plan_date = date(2026, 5, 1)
    with SessionFactory() as s:
        _mk_task(s, due_date=plan_date)
        s.commit()
        sender = _Sender()
        send_evening_plan(s, sender=sender, plan_date=plan_date, user_ids=["U-owner"])
        s.commit()

    assert len(sender.posts) == 1

    with SessionFactory() as s:
        report = send_evening_plan(
            s, sender=sender, plan_date=plan_date, user_ids=["U-owner"]
        )
        s.commit()
    # Second call short-circuits via audit_logs.
    assert report.skipped_idempotent == 1
    assert len(sender.posts) == 1


# --------------------------------------------------------------------------- #
# Skip + Approve handlers
# --------------------------------------------------------------------------- #


def _skip_body(plan_date: date, task_id: int, user="U-owner"):
    return {
        "actions": [
            {
                "action_id": bk.ACTION_PLAN_SKIP,
                "value": f"{plan_date.isoformat()}|{task_id}",
            }
        ],
        "user": {"id": user},
        "channel": {"id": "DM-1"},
        "message": {"ts": "100.0"},
    }


def test_skip_button_marks_item_excluded(patched_session_scope, SessionFactory, ack):
    plan_date = date(2026, 5, 1)
    with SessionFactory() as s:
        t = _mk_task(s, due_date=plan_date)
        s.add(DailyPlanItem(user_id="U-owner", plan_date=plan_date, task_id=t.id))
        s.commit()
        tid = t.id

    handle_plan_skip(body=_skip_body(plan_date, tid), sender=_Sender(), ack=ack)
    with SessionFactory() as s:
        item = (
            s.query(DailyPlanItem)
            .filter(
                DailyPlanItem.user_id == "U-owner",
                DailyPlanItem.task_id == tid,
            )
            .one()
        )
        assert item.excluded_at is not None


def test_skip_does_not_affect_other_users_items(
    patched_session_scope, SessionFactory, ack
):
    plan_date = date(2026, 5, 1)
    with SessionFactory() as s:
        t = _mk_task(s, due_date=plan_date)
        s.add(DailyPlanItem(user_id="U-other", plan_date=plan_date, task_id=t.id))
        s.commit()
        tid = t.id
    # U-owner cannot skip U-other's plan item.
    handle_plan_skip(body=_skip_body(plan_date, tid, user="U-owner"), sender=_Sender(), ack=ack)
    with SessionFactory() as s:
        item = s.query(DailyPlanItem).one()
        assert item.excluded_at is None


def test_approve_button_writes_audit_row(patched_session_scope, SessionFactory, ack):
    plan_date = date(2026, 5, 1)
    body = {
        "actions": [
            {"action_id": bk.ACTION_PLAN_APPROVE, "value": plan_date.isoformat()}
        ],
        "user": {"id": "U-owner"},
        "channel": {"id": "DM-1"},
        "message": {"ts": "100.0"},
    }
    handle_plan_approve(body=body, sender=_Sender(), ack=ack)
    with SessionFactory() as s:
        row = (
            s.query(AuditLog)
            .filter(
                AuditLog.category == "daily_plan", AuditLog.action == "approved"
            )
            .one()
        )
        assert row.actor == "U-owner"
        assert row.payload["plan_date"] == plan_date.isoformat()


# --------------------------------------------------------------------------- #
# Morning: only non-excluded items, with task cards
# --------------------------------------------------------------------------- #


def test_morning_omits_excluded_items(patched_session_scope, SessionFactory):
    plan_date = date(2026, 5, 1)
    with SessionFactory() as s:
        t_keep = _mk_task(s, due_date=plan_date, title="keep")
        t_skip = _mk_task(s, due_date=plan_date, title="skip")
        s.add(DailyPlanItem(user_id="U-owner", plan_date=plan_date, task_id=t_keep.id))
        s.add(
            DailyPlanItem(
                user_id="U-owner",
                plan_date=plan_date,
                task_id=t_skip.id,
                excluded_at=datetime.now(timezone.utc),
            )
        )
        s.commit()
        sender = _Sender()
        send_morning_plan(
            s, sender=sender, plan_date=plan_date, user_ids=["U-owner"]
        )
        s.commit()

    flat = str(sender.posts[0]["blocks"])
    assert f"#{t_keep.id}" in flat
    assert f"#{t_skip.id}" not in flat


def test_morning_includes_task_card_with_start_button(
    patched_session_scope, SessionFactory
):
    plan_date = date(2026, 5, 1)
    with SessionFactory() as s:
        t = _mk_task(s, due_date=plan_date)
        s.add(DailyPlanItem(user_id="U-owner", plan_date=plan_date, task_id=t.id))
        s.commit()
        sender = _Sender()
        send_morning_plan(
            s, sender=sender, plan_date=plan_date, user_ids=["U-owner"]
        )
        s.commit()

    flat = str(sender.posts[0]["blocks"])
    # task_card embeds the existing "Start" button, so the morning DM
    # is actionable without us re-implementing lifecycle controls.
    assert bk.ACTION_START_WORK in flat


def test_morning_idempotent(patched_session_scope, SessionFactory):
    plan_date = date(2026, 5, 1)
    with SessionFactory() as s:
        t = _mk_task(s, due_date=plan_date)
        s.add(DailyPlanItem(user_id="U-owner", plan_date=plan_date, task_id=t.id))
        s.commit()
        sender = _Sender()
        send_morning_plan(
            s, sender=sender, plan_date=plan_date, user_ids=["U-owner"]
        )
        s.commit()

    with SessionFactory() as s:
        report = send_morning_plan(
            s, sender=sender, plan_date=plan_date, user_ids=["U-owner"]
        )
        s.commit()
    assert report.skipped_idempotent == 1
    assert len(sender.posts) == 1


# --------------------------------------------------------------------------- #
# Tracking section in morning DM
# --------------------------------------------------------------------------- #


def test_morning_dm_lists_tracking_subscriptions(
    patched_session_scope, SessionFactory
):
    plan_date = date(2026, 5, 1)
    with SessionFactory() as s:
        own = _mk_task(s, due_date=plan_date)
        watched = _mk_task(s, owner_user_id="U-other", title="watched-task")
        s.add(TaskSubscription(task_id=watched.id, slack_user_id="U-owner"))
        s.add(
            DailyPlanItem(user_id="U-owner", plan_date=plan_date, task_id=own.id)
        )
        s.commit()
        sender = _Sender()
        send_morning_plan(
            s, sender=sender, plan_date=plan_date, user_ids=["U-owner"]
        )
        s.commit()

    flat = str(sender.posts[0]["blocks"])
    assert "Отслеживаемые" in flat
    assert f"#{watched.id}" in flat
