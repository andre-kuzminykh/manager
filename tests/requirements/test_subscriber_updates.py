"""FR-CR-05-02 — subscriber updates throughout the day.

The dispatcher must:
- DM every non-owner subscriber when a Task transitions or gets
  edited.
- Route by user-id shape (numeric → Telegram, U…/W… → Slack).
- Skip recipients we have no sender for (logging only).
- Be idempotent per (task_id, recipient, transition_id) — replay
  must not double-DM.
- Be wired so that calling :meth:`TransitionService.apply` is
  enough to fire the fanout (no caller boilerplate).
"""
from __future__ import annotations

from datetime import date

import pytest

from app.models import (
    AuditLog,
    Task,
    TaskPriority,
    TaskSourceKind,
    TaskStatus,
)
from app.services.subscriber_updates import (
    SubscriberDispatcher,
    dispatch_status_change,
    set_active_dispatcher,
)
from app.services.subscriptions import SubscriptionService
from app.services.transitions import TransitionService


@pytest.fixture(autouse=True)
def _reset_dispatcher():
    """Make sure no other test's `set_active_dispatcher` state leaks
    into ours. The module-level holder is global per process; an
    accidentally-set real Slack client from an unrelated suite would
    otherwise try to POST to api.slack.com (sandbox-blocked) when
    `TransitionService.apply` triggers the fanout."""
    set_active_dispatcher(None)
    yield
    set_active_dispatcher(None)


# --------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------- #


class _FakeSlack:
    def __init__(self) -> None:
        self.posts: list[dict] = []

    def chat_postMessage(self, *, channel: str, text: str):
        self.posts.append({"channel": channel, "text": text})
        return {"ok": True}


class _FakeTG:
    def __init__(self) -> None:
        self.posts: list[dict] = []

    def send_message(self, *, chat_id, text, **kw):
        self.posts.append({"chat_id": chat_id, "text": text})
        return {"message_id": 1}


def _mk(session, *, owner: str = "U-OWNER", title: str = "T") -> Task:
    t = Task(
        title=title,
        priority=TaskPriority.medium,
        status=TaskStatus.todo,
        owner_user_id=owner,
        source_kind=TaskSourceKind.slack,
    )
    session.add(t)
    session.flush()
    return t


# --------------------------------------------------------------------- #
# Dispatcher
# --------------------------------------------------------------------- #


def test_dispatch_status_change_fans_out_to_non_owner_subscribers(session):
    t = _mk(session)
    subs = SubscriptionService()
    subs.subscribe(session, task=t, slack_user_id="U-OWNER")  # owner — skipped
    subs.subscribe(session, task=t, slack_user_id="U-FOLLOW1")
    subs.subscribe(session, task=t, slack_user_id="U-FOLLOW2")
    subs.subscribe(session, task=t, slack_user_id="9988776655")  # TG

    slack = _FakeSlack()
    tg = _FakeTG()
    d = SubscriberDispatcher(slack_poster=slack, telegram_sender=tg)

    history = TransitionService().apply.__self__  # noqa — just to check we don't break
    # Trigger a real transition so we have a TaskStatusHistory row.
    history = TransitionService().apply(
        session, task=t, new_status=TaskStatus.in_progress, actor_slack_user_id="U-OWNER"
    )

    sent = d.dispatch_status_change(session, task=t, history=history)

    # Owner is excluded; one Slack DM each + one TG DM.
    assert sent == 3
    assert {p["channel"] for p in slack.posts} == {"U-FOLLOW1", "U-FOLLOW2"}
    assert {p["chat_id"] for p in tg.posts} == {9988776655}


def test_dispatch_is_idempotent_per_transition(session):
    t = _mk(session)
    SubscriptionService().subscribe(
        session, task=t, slack_user_id="U-FOLLOW"
    )

    slack = _FakeSlack()
    d = SubscriberDispatcher(slack_poster=slack)
    history = TransitionService().apply(
        session, task=t, new_status=TaskStatus.in_progress
    )

    sent_first = d.dispatch_status_change(session, task=t, history=history)
    sent_again = d.dispatch_status_change(session, task=t, history=history)

    # Second call sees the audit_log marker and skips the DM.
    assert sent_first == 1
    assert sent_again == 0
    # Slack only received one post.
    assert len(slack.posts) == 1


def test_dispatch_routes_unknown_uid_shape_to_nowhere(session):
    """A subscriber id that is neither numeric nor Slack-shaped
    (e.g. an accidentally-stored email) is dropped with a log line —
    no exception."""
    t = _mk(session)
    SubscriptionService().subscribe(
        session, task=t, slack_user_id="someone@example.com"
    )

    slack = _FakeSlack()
    tg = _FakeTG()
    d = SubscriberDispatcher(slack_poster=slack, telegram_sender=tg)
    history = TransitionService().apply(
        session, task=t, new_status=TaskStatus.in_progress
    )

    sent = d.dispatch_status_change(session, task=t, history=history)
    assert sent == 0
    assert slack.posts == []
    assert tg.posts == []


def test_transition_service_dispatches_via_active_singleton(session):
    """`TransitionService.apply` must trigger the registered active
    dispatcher with no caller boilerplate — that's the whole point
    of FR-CR-05-02 (any future trigger inherits the fanout)."""
    t = _mk(session)
    SubscriptionService().subscribe(
        session, task=t, slack_user_id="U-FOLLOW"
    )

    slack = _FakeSlack()
    set_active_dispatcher(SubscriberDispatcher(slack_poster=slack))
    try:
        TransitionService().apply(
            session, task=t, new_status=TaskStatus.in_progress
        )
        assert len(slack.posts) == 1
        assert slack.posts[0]["channel"] == "U-FOLLOW"
        # Audit log row written.
        rows = (
            session.query(AuditLog)
            .filter(AuditLog.category == "subscriber_update")
            .all()
        )
        assert len(rows) == 1
    finally:
        set_active_dispatcher(None)


def test_dispatch_no_dispatcher_set_is_noop(session):
    """The module-level `dispatch_status_change` is a no-op when
    no active dispatcher is registered (tests / cron jobs)."""
    t = _mk(session)
    SubscriptionService().subscribe(session, task=t, slack_user_id="U-FOLLOW")
    history = TransitionService().apply(
        session, task=t, new_status=TaskStatus.in_progress
    )
    set_active_dispatcher(None)
    out = dispatch_status_change(session, task=t, history=history)
    assert out == 0
