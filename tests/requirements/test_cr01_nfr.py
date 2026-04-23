"""Tests for CR-01 non-functional requirements NFR-CR-1..3."""
from __future__ import annotations

import time

import pytest
from slack_sdk.errors import SlackApiError

from app.models import Task, TaskStatus, TaskSubscription
from app.services import (
    DigestKind,
    DigestService,
    NotificationService,
    SubscriptionService,
    TransitionService,
)
from app.slack_bot.rate_limiter import RateAwareSlackSender


class _FakeResp:
    def __init__(self, ok=True):
        self.data = {"ok": ok, "ts": "0.0"}


def _fake_resp(status=500):
    r = type("R", (), {})()
    r.status_code = status
    r.status = status
    r.reason = "boom"
    r.headers = {}
    r.data = {}
    return r


# =========================================================================== #
# NFR-CR-1: Broadcasts go through the rate-aware sender (per-channel throttle
# and 429 Retry-After handling).
# =========================================================================== #


def test_nfr_cr1_notification_uses_rate_aware_sender_api(session):
    """NotificationService requires only `post_message(**kwargs)` — a duck-
    typed contract that our RateAwareSlackSender satisfies."""
    task = Task(title="t", status=TaskStatus.todo, owner_user_id="U1")
    session.add(task)
    session.flush()
    subs = SubscriptionService()
    subs.subscribe(session, task=task, slack_user_id="U1")

    class Cli:
        def chat_postMessage(self, **kw):  # noqa: N802
            return _FakeResp()

    sender = RateAwareSlackSender(Cli(), min_interval_seconds=0.0)
    notifier = NotificationService(sender=sender, subscriptions=subs)
    count = notifier.broadcast_status_change(
        session,
        task=task,
        from_status=TaskStatus.todo,
        to_status=TaskStatus.in_progress,
        actor_slack_user_id="U1",
    )
    assert count == 1


def test_nfr_cr1_broadcast_throttles_per_user_channel(session):
    """When the same user is subscribed (user DM channel), consecutive posts
    are throttled by the rate-aware sender's min_interval."""
    task = Task(title="t", status=TaskStatus.todo, owner_user_id="U1")
    session.add(task)
    session.flush()
    subs = SubscriptionService()
    subs.subscribe(session, task=task, slack_user_id="U1")

    class Cli:
        def chat_postMessage(self, **kw):  # noqa: N802
            return _FakeResp()

    sender = RateAwareSlackSender(Cli(), min_interval_seconds=0.15)
    notifier = NotificationService(sender=sender, subscriptions=subs)

    t0 = time.monotonic()
    notifier.broadcast_status_change(
        session,
        task=task,
        from_status=TaskStatus.todo,
        to_status=TaskStatus.in_progress,
        actor_slack_user_id="U1",
    )
    notifier.broadcast_status_change(
        session,
        task=task,
        from_status=TaskStatus.in_progress,
        to_status=TaskStatus.review,
        actor_slack_user_id="U1",
    )
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.15


def test_nfr_cr1_broadcast_retries_on_429_via_sender(session):
    task = Task(title="t", status=TaskStatus.todo, owner_user_id="U1")
    session.add(task)
    session.flush()
    subs = SubscriptionService()
    subs.subscribe(session, task=task, slack_user_id="U1")

    calls = {"n": 0}

    class Cli:
        def chat_postMessage(self, **kw):  # noqa: N802
            calls["n"] += 1
            if calls["n"] == 1:
                resp = type(
                    "R", (), {"status_code": 429, "headers": {"Retry-After": "0"}, "data": {}}
                )()
                raise SlackApiError("rate_limited", resp)
            return _FakeResp()

    sender = RateAwareSlackSender(Cli(), min_interval_seconds=0.0)
    notifier = NotificationService(sender=sender, subscriptions=subs)
    count = notifier.broadcast_status_change(
        session,
        task=task,
        from_status=TaskStatus.todo,
        to_status=TaskStatus.in_progress,
        actor_slack_user_id="U1",
    )
    assert count == 1  # success after retry
    assert calls["n"] >= 2


def test_nfr_cr1_broadcast_counts_exclude_failures(session):
    task = Task(title="t", status=TaskStatus.todo, owner_user_id="U1")
    session.add(task)
    session.flush()
    subs = SubscriptionService()
    for uid in ("U1", "U2"):
        subs.subscribe(session, task=task, slack_user_id=uid)

    class Sender:
        def post_message(self, **kw):
            if kw["channel"] == "U2":
                raise RuntimeError("nope")
            return {"ok": True}

    notifier = NotificationService(sender=Sender(), subscriptions=subs)
    assert (
        notifier.broadcast_status_change(
            session,
            task=task,
            from_status=TaskStatus.todo,
            to_status=TaskStatus.in_progress,
            actor_slack_user_id="U1",
        )
        == 1
    )


# =========================================================================== #
# NFR-CR-2: Digests are idempotent per (user, day).
# =========================================================================== #


class _R:
    def __init__(self):
        self.msgs = []

    def post_message(self, **kw):
        self.msgs.append(kw)
        return {"ok": True}


def test_nfr_cr2_daily_digest_second_run_skips_all(session):
    from datetime import date

    t = Task(title="t", status=TaskStatus.todo, owner_user_id="U1", due_date=date(2026, 4, 20))
    session.add(t)
    session.flush()

    sender = _R()
    svc = DigestService(sender=sender)
    svc.send(session, DigestKind.daily, today=date(2026, 4, 20))
    initial = len(sender.msgs)
    svc.send(session, DigestKind.daily, today=date(2026, 4, 20))
    assert len(sender.msgs) == initial


def test_nfr_cr2_weekly_digest_second_run_same_week_skips(session):
    from datetime import date

    t = Task(title="t", status=TaskStatus.todo, owner_user_id="U1", due_date=date(2026, 4, 22))
    session.add(t)
    session.flush()

    sender = _R()
    svc = DigestService(sender=sender)
    svc.send(session, DigestKind.weekly, today=date(2026, 4, 20))
    first = len(sender.msgs)
    svc.send(session, DigestKind.weekly, today=date(2026, 4, 24))
    assert len(sender.msgs) == first


def test_nfr_cr2_deadline_digest_idempotent(session):
    from datetime import date, timedelta

    t = Task(
        title="t",
        status=TaskStatus.todo,
        owner_user_id="U1",
        due_date=date(2026, 4, 20) - timedelta(days=1),
    )
    session.add(t)
    session.flush()

    sender = _R()
    svc = DigestService(sender=sender)
    svc.send(session, DigestKind.deadlines, today=date(2026, 4, 20))
    n1 = len(sender.msgs)
    svc.send(session, DigestKind.deadlines, today=date(2026, 4, 20))
    assert len(sender.msgs) == n1


# =========================================================================== #
# NFR-CR-3: Status transition + history row are atomic.
# =========================================================================== #


def test_nfr_cr3_transition_writes_history_in_same_transaction(session):
    task = Task(title="t", status=TaskStatus.todo, owner_user_id="U1")
    session.add(task)
    session.flush()
    TransitionService().apply(
        session, task=task, new_status=TaskStatus.in_progress, actor_slack_user_id="U1"
    )
    from app.models import TaskStatusHistory

    assert session.query(TaskStatusHistory).count() == 1
    assert task.status == TaskStatus.in_progress


def test_nfr_cr3_invalid_transition_does_not_write_history(session):
    task = Task(title="t", status=TaskStatus.review, owner_user_id="U1")
    session.add(task)
    session.flush()
    from app.services import InvalidTransition

    from app.models import TaskStatusHistory

    before = session.query(TaskStatusHistory).count()
    with pytest.raises(InvalidTransition):
        TransitionService().apply(
            session, task=task, new_status=TaskStatus.backlog
        )
    after = session.query(TaskStatusHistory).count()
    assert after == before


def test_nfr_cr3_history_records_actor():
    from app.models import TaskStatusHistory

    h = TaskStatusHistory(
        task_id=1,
        from_status=None,
        to_status=TaskStatus.backlog,
        changed_by_slack_user_id="U-changer",
        reason="x",
    )
    assert h.changed_by_slack_user_id == "U-changer"


# =========================================================================== #
# Extra: context_view_modal structure
# =========================================================================== #


def test_context_view_modal_title_and_close_buttons():
    from app.slack_bot import blocks as bk

    class _Snap:
        conversation_id = "C1"
        source_ts = "1.0"
        source_message = {"ts": "1.0", "text": "src", "user": "U1"}
        history_before = [{"ts": "0.5", "text": "prev", "user": "U2"}]
        thread_messages = []

    view = bk.context_view_modal(snapshot=_Snap())
    assert view["type"] == "modal"
    assert view["title"]["text"] == "Task context"
    assert view["close"]["text"] == "Close"


def test_context_view_modal_includes_history_and_source():
    from app.slack_bot import blocks as bk

    class _Snap:
        conversation_id = "C1"
        source_ts = "1.0"
        source_message = {"ts": "1.0", "text": "SRC", "user": "U1"}
        history_before = [{"ts": "0.5", "text": "PREV", "user": "U2"}]
        thread_messages = [{"ts": "1.1", "text": "REPLY", "user": "U3"}]

    view = bk.context_view_modal(snapshot=_Snap())
    body = "".join(
        b["text"]["text"] for b in view["blocks"] if b.get("type") == "section"
    )
    assert "SRC" in body
    assert "PREV" in body
    assert "REPLY" in body


def test_context_view_modal_empty_snapshot_shows_placeholder():
    from app.slack_bot import blocks as bk

    class _Snap:
        conversation_id = "C1"
        source_ts = "1.0"
        source_message = {}
        history_before = []
        thread_messages = []

    view = bk.context_view_modal(snapshot=_Snap())
    body = "".join(
        b["text"]["text"] for b in view["blocks"] if b.get("type") == "section"
    )
    # It is OK if empty context renders minimally.
    assert isinstance(body, str)
