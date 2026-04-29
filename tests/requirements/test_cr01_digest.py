"""Tests for CR-01 FR-CR-6 (digests + deadline reminders) and NFR-CR-2
(idempotent digest delivery)."""
from __future__ import annotations

from datetime import date, timedelta

from app.models import AuditLog, Task, TaskStatus
from app.services import DigestKind, DigestService


class _RecordingSender:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def post_message(self, **kw):
        self.messages.append(kw)
        return {"ok": True}


def _task(session, *, owner="U1", status=TaskStatus.todo, due=None, title="t", minutes=None):
    t = Task(
        title=title,
        owner_user_id=owner,
        status=status,
        due_date=due,
        estimated_minutes=minutes,
    )
    session.add(t)
    session.flush()
    return t


# =========================================================================== #
# Daily digest
# =========================================================================== #


def test_daily_digest_sent_to_each_owner_once(session):
    today = date(2026, 4, 20)
    _task(session, owner="U1", status=TaskStatus.todo, due=today, title="today")
    _task(session, owner="U2", status=TaskStatus.todo, due=today, title="today2")

    sender = _RecordingSender()
    report = DigestService(sender=sender).send(session, DigestKind.daily, today=today)
    assert report.recipients == 2
    channels = [m["channel"] for m in sender.messages]
    assert set(channels) == {"U1", "U2"}


def test_daily_digest_lists_today_only(session):
    """FR-CR-05-01 — morning digest narrowed to «Today's tasks»
    only. Approaching / Overdue moved to dedicated channels."""
    today = date(2026, 4, 20)
    _task(session, owner="U1", due=today, title="do_today")
    _task(session, owner="U1", due=today + timedelta(days=1), title="tomorrow")
    _task(session, owner="U1", due=today + timedelta(days=2), title="after_tomorrow")
    _task(session, owner="U1", due=today - timedelta(days=1), title="overdue")

    sender = _RecordingSender()
    DigestService(sender=sender).send(session, DigestKind.daily, today=today)
    body = "\n".join(
        b["text"]["text"]
        for b in sender.messages[0]["blocks"]
        if b.get("type") == "section"
    )
    # Today's task is shown.
    assert "do_today" in body
    # Approaching / Overdue tasks are NOT in the morning digest anymore.
    assert "tomorrow" not in body
    assert "after_tomorrow" not in body
    assert "overdue" not in body


def test_daily_digest_skipped_idempotently_on_second_run(session):
    today = date(2026, 4, 20)
    _task(session, owner="U1", due=today)
    sender = _RecordingSender()
    svc = DigestService(sender=sender)
    r1 = svc.send(session, DigestKind.daily, today=today)
    r2 = svc.send(session, DigestKind.daily, today=today)
    assert r1.recipients == 1
    assert r2.skipped_idempotent == 1
    assert len(sender.messages) == 1


def test_daily_digest_new_day_resends(session):
    _task(session, owner="U1", due=date(2026, 4, 21))
    sender = _RecordingSender()
    svc = DigestService(sender=sender)
    svc.send(session, DigestKind.daily, today=date(2026, 4, 20))
    svc.send(session, DigestKind.daily, today=date(2026, 4, 21))
    assert len(sender.messages) == 2


def test_daily_digest_ignores_done_tasks(session):
    today = date(2026, 4, 20)
    _task(session, owner="U1", status=TaskStatus.done, due=today)
    sender = _RecordingSender()
    report = DigestService(sender=sender).send(session, DigestKind.daily, today=today)
    # Done tasks are not open, so U1 has no open tasks → not a recipient.
    assert report.recipients == 0


# =========================================================================== #
# Weekly digest
# =========================================================================== #


def test_weekly_digest_covers_current_week(session):
    # Monday 2026-04-20; week ends 2026-04-26.
    monday = date(2026, 4, 20)
    _task(session, owner="U1", due=monday, title="mon")
    _task(session, owner="U1", due=monday + timedelta(days=3), title="thu")
    _task(session, owner="U1", due=monday + timedelta(days=8), title="next_week")

    sender = _RecordingSender()
    DigestService(sender=sender).send(session, DigestKind.weekly, today=monday)
    body = sender.messages[0]["blocks"][0]["text"]["text"]
    assert "mon" in body
    assert "thu" in body
    assert "next_week" not in body


def test_weekly_digest_idempotent_same_week(session):
    _task(session, owner="U1", due=date(2026, 4, 21))
    sender = _RecordingSender()
    svc = DigestService(sender=sender)
    svc.send(session, DigestKind.weekly, today=date(2026, 4, 20))
    svc.send(session, DigestKind.weekly, today=date(2026, 4, 22))  # same week
    assert len(sender.messages) == 1


def test_weekly_digest_highlights_attention_items(session):
    monday = date(2026, 4, 20)
    _task(session, owner="U1", status=TaskStatus.in_progress, title="in_prog")
    sender = _RecordingSender()
    DigestService(sender=sender).send(session, DigestKind.weekly, today=monday)
    body = sender.messages[0]["blocks"][0]["text"]["text"]
    assert "in_prog" in body


# =========================================================================== #
# Deadline reminders
# =========================================================================== #


def test_deadline_reminder_sent_for_overdue(session):
    today = date(2026, 4, 20)
    _task(session, owner="U1", due=today - timedelta(days=1), title="overdue_task")
    sender = _RecordingSender()
    DigestService(sender=sender).send(session, DigestKind.deadlines, today=today)
    assert any("overdue_task" in m["blocks"][0]["text"]["text"] for m in sender.messages)


def test_deadline_reminder_sent_for_approaching(session):
    today = date(2026, 4, 20)
    _task(session, owner="U1", due=today + timedelta(days=1), title="due_soon")
    sender = _RecordingSender()
    DigestService(sender=sender).send(session, DigestKind.deadlines, today=today)
    assert any("due_soon" in m["blocks"][0]["text"]["text"] for m in sender.messages)


def test_deadline_reminder_skips_far_future(session):
    today = date(2026, 4, 20)
    _task(session, owner="U1", due=today + timedelta(days=30), title="far")
    sender = _RecordingSender()
    report = DigestService(sender=sender).send(session, DigestKind.deadlines, today=today)
    assert report.recipients == 0


def test_deadline_reminder_idempotent(session):
    today = date(2026, 4, 20)
    _task(session, owner="U1", due=today - timedelta(days=1))
    sender = _RecordingSender()
    svc = DigestService(sender=sender)
    svc.send(session, DigestKind.deadlines, today=today)
    svc.send(session, DigestKind.deadlines, today=today)
    assert len(sender.messages) == 1


def test_deadline_reminder_writes_audit_log(session):
    today = date(2026, 4, 20)
    _task(session, owner="U1", due=today + timedelta(days=1))
    sender = _RecordingSender()
    DigestService(sender=sender).send(session, DigestKind.deadlines, today=today)
    logs = session.query(AuditLog).filter(AuditLog.category == "digest").all()
    assert logs
    assert logs[0].action.startswith("deadline:")


def test_daily_digest_audit_log_records_counts(session):
    today = date(2026, 4, 20)
    _task(session, owner="U1", due=today)
    sender = _RecordingSender()
    DigestService(sender=sender).send(session, DigestKind.daily, today=today)
    logs = session.query(AuditLog).filter(AuditLog.category == "digest").all()
    assert any(l.payload.get("today") == 1 for l in logs)


def test_weekly_audit_log_records_planned_count(session):
    monday = date(2026, 4, 20)
    _task(session, owner="U1", due=monday + timedelta(days=2))
    sender = _RecordingSender()
    DigestService(sender=sender).send(session, DigestKind.weekly, today=monday)
    logs = session.query(AuditLog).filter(AuditLog.category == "digest").all()
    assert any("planned" in (l.payload or {}) for l in logs)


def test_deadline_digest_audit_log_records_overdue_flag(session):
    today = date(2026, 4, 20)
    _task(session, owner="U1", due=today - timedelta(days=2))
    sender = _RecordingSender()
    DigestService(sender=sender).send(session, DigestKind.deadlines, today=today)
    logs = session.query(AuditLog).filter(AuditLog.category == "digest").all()
    assert any(l.payload.get("overdue") is True for l in logs)
