"""FR-CR-04-29 — Telegram-side digests / plans / reminders.

Tests cover the per-user filter (numeric uid → TG, otherwise →
Slack), idempotency via audit_logs, and the basic content of each
DM/post. The TelegramSender is replaced with a recorder so we
don't hit api.telegram.org.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import pytest

from app.models import (
    AuditLog,
    DailyPlanItem,
    Task,
    TaskPriority,
    TaskSourceKind,
    TaskStatus,
    TaskSubscription,
)
from app.telegram_bot import notifications as tn


@dataclass
class _RecordingSender:
    enabled: bool = True
    sent: list[dict] = field(default_factory=list)

    def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return {"message_id": 1}

    def update_message(self, **kwargs):
        return {}

    def delete_message(self, **kwargs):
        return {}

    def answer_callback_query(self, **kwargs):
        return {}


def _mk(session, **kw) -> int:
    base = dict(
        title="t",
        priority=TaskPriority.medium,
        status=TaskStatus.in_progress,
        owner_user_id="11111",
        source_kind=TaskSourceKind.telegram,
    )
    base.update(kw)
    t = Task(**base)
    session.add(t)
    session.flush()
    return t.id


# --------------------------------------------------------------------------- #
# Channel filter
# --------------------------------------------------------------------------- #


def test_telegram_user_id_detection():
    assert tn._is_telegram_user_id("12345")
    assert tn._is_telegram_user_id("-12345")
    assert not tn._is_telegram_user_id("U09ABCD")
    assert not tn._is_telegram_user_id(None)
    assert not tn._is_telegram_user_id("")


def test_telegram_owner_ids_only_lists_numeric(session):
    _mk(session, owner_user_id="555")  # TG
    _mk(session, owner_user_id="U09SLACK")  # Slack
    _mk(session, owner_user_id="999")  # TG
    out = tn._telegram_owner_ids(session)
    assert set(out) == {"555", "999"}


def test_telegram_owner_ids_skips_deleted_and_done(session):
    from datetime import datetime, timezone

    _mk(session, owner_user_id="111", status=TaskStatus.done)
    _mk(
        session,
        owner_user_id="222",
        deleted_at=datetime.now(timezone.utc),
    )
    _mk(session, owner_user_id="333", status=TaskStatus.todo)
    assert tn._telegram_owner_ids(session) == ["333"]


# --------------------------------------------------------------------------- #
# Morning digest
# --------------------------------------------------------------------------- #


def test_morning_digest_today_only(
    patched_session_scope, SessionFactory
):
    """FR-CR-05-01 — morning DM is just «Today» now. Overdue moves
    to the per-task deadline reminder + the evening 3-section DM."""
    today = date(2026, 5, 1)
    sender = _RecordingSender()
    with SessionFactory() as s:
        _mk(s, owner_user_id="555", title="due_today", due_date=today)
        _mk(
            s,
            owner_user_id="555",
            title="late",
            due_date=today - timedelta(days=2),
        )
        s.commit()

    with SessionFactory() as s:
        report = tn.send_morning_digest(s, sender=sender, today=today)
        s.commit()
    assert report.recipients == 1
    assert sender.sent
    body = sender.sent[0]["text"]
    assert "due_today" in body
    # Overdue / Approaching no longer in the morning DM.
    assert "Overdue" not in body
    assert "late" not in body


def test_starts_now_dms_owner_when_start_time_is_now(
    patched_session_scope, SessionFactory
):
    """FR-CR-05-03 — a Task with start_date=today and start_time
    within the [now-5m, now] window triggers a `🚀 Starting now` DM
    to the owner. Outside the window: no DM."""
    from datetime import datetime, time as _time

    today = date.today()
    now = datetime.now()
    sender = _RecordingSender()
    with SessionFactory() as s:
        # Inside the window — start_time is "now-2m".
        inside = (now - timedelta(minutes=2)).time().replace(microsecond=0)
        _mk(
            s,
            owner_user_id="555",
            title="due_now",
            start_date=today,
            start_time=inside,
        )
        # Outside the window — scheduled hours from now.
        outside = (now + timedelta(hours=2)).time().replace(microsecond=0)
        _mk(
            s,
            owner_user_id="555",
            title="later",
            start_date=today,
            start_time=outside,
        )
        s.commit()
    with SessionFactory() as s:
        report = tn.send_starts_now(s, sender=sender, today=today)
        s.commit()
    assert report.recipients == 1
    assert len(sender.sent) == 1
    body = sender.sent[0]["text"]
    assert "Starting now" in body
    assert "due_now" in body
    assert "later" not in body


def test_starts_now_idempotent(patched_session_scope, SessionFactory):
    """A second call within the same window is a no-op."""
    from datetime import datetime

    today = date.today()
    inside = (datetime.now() - timedelta(minutes=1)).time().replace(microsecond=0)
    sender = _RecordingSender()
    with SessionFactory() as s:
        _mk(
            s,
            owner_user_id="555",
            start_date=today,
            start_time=inside,
        )
        s.commit()
    with SessionFactory() as s:
        tn.send_starts_now(s, sender=sender, today=today)
        s.commit()
    with SessionFactory() as s:
        report = tn.send_starts_now(s, sender=sender, today=today)
        s.commit()
    assert report.skipped_idempotent == 1


def test_morning_digest_idempotent(patched_session_scope, SessionFactory):
    today = date(2026, 5, 1)
    sender = _RecordingSender()
    with SessionFactory() as s:
        _mk(s, owner_user_id="555", due_date=today)
        s.commit()
    with SessionFactory() as s:
        tn.send_morning_digest(s, sender=sender, today=today)
        s.commit()
    with SessionFactory() as s:
        report = tn.send_morning_digest(s, sender=sender, today=today)
        s.commit()
    assert report.skipped_idempotent == 1


def test_morning_digest_skips_user_with_no_open_tasks(
    patched_session_scope, SessionFactory
):
    today = date(2026, 5, 1)
    sender = _RecordingSender()
    with SessionFactory() as s:
        _mk(s, owner_user_id="555", status=TaskStatus.done, due_date=today)
        s.commit()
    with SessionFactory() as s:
        report = tn.send_morning_digest(s, sender=sender, today=today)
        s.commit()
    # No TG owners with open tasks → 0 recipients.
    assert report.recipients == 0
    assert sender.sent == []


# --------------------------------------------------------------------------- #
# Daily plan
# --------------------------------------------------------------------------- #


def test_evening_plan_persists_items_and_dms_owner(
    patched_session_scope, SessionFactory
):
    plan_date = date(2026, 5, 2)
    sender = _RecordingSender()
    with SessionFactory() as s:
        _mk(s, owner_user_id="555", due_date=plan_date)
        s.commit()
    with SessionFactory() as s:
        report = tn.send_evening_plan(s, sender=sender, plan_date=plan_date)
        s.commit()
    assert report.recipients == 1
    with SessionFactory() as s:
        items = s.query(DailyPlanItem).filter_by(user_id="555").all()
        assert len(items) == 1
        assert items[0].plan_date == plan_date


def test_evening_plan_includes_three_sections(
    patched_session_scope, SessionFactory
):
    """FR-CR-05-04 — evening DM packs Done today + Subscriptions
    update + Tomorrow's plan in one message."""
    from app.models import (
        TaskStatusHistory,
        TaskSubscription,
    )

    plan_date = date(2026, 5, 2)
    today = plan_date - timedelta(days=1)
    sender = _RecordingSender()
    with SessionFactory() as s:
        # Owner = "555" (TG numeric id). They closed «closed_today»
        # earlier today, are subscribed to «watching» (owned by
        # someone else), and have «for_tomorrow» due tomorrow.
        from datetime import datetime, timezone

        closed_today = _mk(
            s,
            owner_user_id="555",
            title="closed_today",
            status=TaskStatus.done,
            due_date=today,
        )
        s.add(
            TaskStatusHistory(
                task_id=closed_today,
                from_status=TaskStatus.in_progress,
                to_status=TaskStatus.done,
                at=datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
                + timedelta(hours=10),
            )
        )
        watching = _mk(
            s,
            owner_user_id="U999",  # someone else, Slack-shaped so they
            # don't show up as a TG-owner recipient themselves
            title="watching",
            status=TaskStatus.in_progress,
        )
        s.add(TaskSubscription(task_id=watching, slack_user_id="555"))
        _mk(
            s,
            owner_user_id="555",
            title="for_tomorrow",
            status=TaskStatus.todo,
            due_date=plan_date,
        )
        s.commit()
    with SessionFactory() as s:
        report = tn.send_evening_plan(s, sender=sender, plan_date=plan_date)
        s.commit()
    assert report.recipients == 1
    body = sender.sent[0]["text"]
    # All three section headers + their content.
    assert "Done today" in body and "closed_today" in body
    assert "Subscriptions update" in body and "watching" in body
    assert f"Plan for {plan_date.isoformat()}" in body and "for_tomorrow" in body


def test_morning_plan_only_runs_when_evening_seeded_items(
    patched_session_scope, SessionFactory
):
    plan_date = date(2026, 5, 2)
    sender = _RecordingSender()
    with SessionFactory() as s:
        report = tn.send_morning_plan(s, sender=sender, plan_date=plan_date)
        s.commit()
    assert report.recipients == 0


# --------------------------------------------------------------------------- #
# Deadline + thread reminders
# --------------------------------------------------------------------------- #


def test_deadline_reminder_per_task_dedup_per_day(
    patched_session_scope, SessionFactory
):
    today = date(2026, 5, 1)
    sender = _RecordingSender()
    with SessionFactory() as s:
        _mk(
            s,
            owner_user_id="555",
            due_date=today + timedelta(days=1),
            title="approaching",
        )
        s.commit()
    with SessionFactory() as s:
        report = tn.send_deadline_reminders(s, sender=sender, today=today)
        s.commit()
    assert report.recipients == 1
    with SessionFactory() as s:
        report = tn.send_deadline_reminders(s, sender=sender, today=today)
        s.commit()
    assert report.skipped_idempotent == 1


def test_thread_reminders_post_to_source_chat(
    patched_session_scope, SessionFactory
):
    today = date(2026, 5, 1)
    sender = _RecordingSender()
    with SessionFactory() as s:
        _mk(
            s,
            owner_user_id="555",
            status=TaskStatus.in_progress,
            source_conversation_id="-1001234",
            source_message_ts="42",
        )
        s.commit()
    with SessionFactory() as s:
        report = tn.send_thread_reminders(s, sender=sender, today=today)
        s.commit()
    assert report.recipients == 1
    msg = sender.sent[0]
    assert msg["chat_id"] == -1001234
    assert msg.get("reply_to_message_id") == 42


# --------------------------------------------------------------------------- #
# Admin watch-list
# --------------------------------------------------------------------------- #


def test_admin_watchlist_dms_each_admin(
    patched_session_scope, SessionFactory, monkeypatch
):
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "777,888")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        today = date(2026, 5, 1)
        sender = _RecordingSender()
        with SessionFactory() as s:
            _mk(
                s,
                owner_user_id="555",
                status=TaskStatus.in_progress,
                title="alive",
            )
            s.commit()
        with SessionFactory() as s:
            report = tn.send_admin_watchlist(s, sender=sender, today=today)
            s.commit()
        assert report.recipients == 2
        admins_dm = {m["chat_id"] for m in sender.sent}
        assert admins_dm == {777, 888}
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_admin_watchlist_no_admins_no_messages(
    patched_session_scope, SessionFactory, monkeypatch
):
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        sender = _RecordingSender()
        with SessionFactory() as s:
            report = tn.send_admin_watchlist(s, sender=sender)
            s.commit()
        assert report.recipients == 0
        assert sender.sent == []
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]
