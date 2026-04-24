"""Tests for CR-01: lifecycle (backlog/todo/in_progress/review/done),
TransitionService, persistence defaults, start_work button, status history."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.models import (
    ActionDraft,
    ActionDraftState,
    ContextSnapshot,
    IntentInference,
    Task,
    TaskStatus,
    TaskStatusHistory,
    TaskSubscription,
)
from app.models.intent import IntentType as IE
from app.persistence import create_task_from_draft
from app.services import (
    ALLOWED_TRANSITIONS,
    InvalidTransition,
    NotificationService,
    SubscriptionService,
    TransitionService,
)
from app.slack_bot import blocks as bk


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _mk_draft(session, *, payload):
    snap = ContextSnapshot(
        conversation_id="C1",
        source_ts="1.0",
        source_message={"ts": "1.0", "text": "x", "user": "U1"},
        history_before=[],
        thread_messages=[],
    )
    session.add(snap)
    session.flush()
    inf = IntentInference(
        context_snapshot_id=snap.id,
        intent=IE.create_task,
        confidence=0.9,
        invocation_type="mention",
    )
    session.add(inf)
    session.flush()
    draft = ActionDraft(
        inference_id=inf.id,
        intent=IE.create_task,
        state=ActionDraftState.proposed,
        payload=payload,
        created_by_slack_user_id="U-creator",
        slack_message_ts="1.0",
    )
    session.add(draft)
    session.flush()
    return draft, snap


def _make_task(
    session,
    *,
    status: TaskStatus = TaskStatus.todo,
    owner: str | None = "U-owner",
    due: date | None = None,
    estimated_minutes: int | None = None,
) -> Task:
    t = Task(
        title="t",
        status=status,
        owner_user_id=owner,
        due_date=due,
        estimated_minutes=estimated_minutes,
    )
    session.add(t)
    session.flush()
    return t


# =========================================================================== #
# FR-CR-3: lifecycle enum values
# =========================================================================== #


def test_fr_cr3_status_enum_has_exactly_five_values():
    assert {s.value for s in TaskStatus} == {
        "backlog",
        "todo",
        "in_progress",
        "review",
        "done",
    }


@pytest.mark.parametrize(
    "value",
    ["backlog", "todo", "in_progress", "review", "done"],
)
def test_fr_cr3_enum_accepts_each_value(value):
    assert TaskStatus(value).value == value


def test_fr_cr3_old_values_are_gone():
    for old in ("open", "cancelled"):
        with pytest.raises(ValueError):
            TaskStatus(old)


def test_fr_cr3_allowed_transitions_graph_matches_spec():
    assert TaskStatus.todo in ALLOWED_TRANSITIONS[TaskStatus.backlog]
    assert TaskStatus.in_progress in ALLOWED_TRANSITIONS[TaskStatus.todo]
    assert TaskStatus.review in ALLOWED_TRANSITIONS[TaskStatus.in_progress]
    assert TaskStatus.done in ALLOWED_TRANSITIONS[TaskStatus.review]


@pytest.mark.parametrize(
    "old, new",
    [
        (TaskStatus.backlog, TaskStatus.todo),
        (TaskStatus.backlog, TaskStatus.in_progress),
        (TaskStatus.todo, TaskStatus.in_progress),
        (TaskStatus.in_progress, TaskStatus.review),
        (TaskStatus.review, TaskStatus.done),
        (TaskStatus.done, TaskStatus.in_progress),
    ],
)
def test_fr_cr3_allowed_transitions_apply(session, old, new):
    task = _make_task(session, status=old)
    TransitionService().apply(session, task=task, new_status=new, actor_slack_user_id="U1")
    assert task.status == new


@pytest.mark.parametrize(
    "old, new",
    [
        (TaskStatus.backlog, TaskStatus.review),
        (TaskStatus.review, TaskStatus.todo),
        (TaskStatus.todo, TaskStatus.review),  # must go via in_progress
    ],
)
def test_fr_cr3_disallowed_transitions_raise(session, old, new):
    task = _make_task(session, status=old)
    with pytest.raises(InvalidTransition):
        TransitionService().apply(session, task=task, new_status=new)


def test_fr_cr3_transition_to_same_state_raises(session):
    task = _make_task(session, status=TaskStatus.todo)
    with pytest.raises(InvalidTransition):
        TransitionService().apply(session, task=task, new_status=TaskStatus.todo)


# =========================================================================== #
# FR-CR-3 / FR-CR-7: initial status on persist + history
# =========================================================================== #


def test_fr_cr3_task_without_due_date_starts_in_backlog(session):
    draft, snap = _mk_draft(session, payload={"title": "t"})
    t = create_task_from_draft(
        session,
        draft=draft,
        source={"conversation_id": "C1", "message_ts": "1.0"},
        context_snapshot_id=snap.id,
        fallback_author_slack_id="U1",
    )
    assert t.status == TaskStatus.backlog
    assert t.is_current_week is False


def test_fr_cr3_task_with_near_due_date_starts_in_todo(session):
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    draft, snap = _mk_draft(session, payload={"title": "t", "due_date": tomorrow})
    t = create_task_from_draft(
        session,
        draft=draft,
        source={"conversation_id": "C1", "message_ts": "1.0"},
        context_snapshot_id=snap.id,
        fallback_author_slack_id="U1",
    )
    assert t.status == TaskStatus.todo
    assert t.is_current_week is True


def test_fr_cr3_task_with_far_due_date_stays_backlog(session):
    far = (date.today() + timedelta(days=30)).isoformat()
    draft, snap = _mk_draft(session, payload={"title": "t", "due_date": far})
    t = create_task_from_draft(
        session,
        draft=draft,
        source={},
        context_snapshot_id=snap.id,
        fallback_author_slack_id="U1",
    )
    assert t.status == TaskStatus.backlog
    assert t.is_current_week is False


def test_fr_cr7_initial_history_row_recorded_on_create(session):
    draft, snap = _mk_draft(session, payload={"title": "t"})
    t = create_task_from_draft(
        session,
        draft=draft,
        source={},
        context_snapshot_id=snap.id,
        fallback_author_slack_id="U1",
    )
    history = session.query(TaskStatusHistory).filter_by(task_id=t.id).all()
    assert len(history) == 1
    assert history[0].from_status is None
    assert history[0].to_status == TaskStatus.backlog
    assert history[0].reason == "created"


def test_fr_cr7_history_row_written_per_transition(session):
    task = _make_task(session, status=TaskStatus.todo)
    TransitionService().apply(
        session, task=task, new_status=TaskStatus.in_progress, actor_slack_user_id="U1"
    )
    TransitionService().apply(
        session, task=task, new_status=TaskStatus.review, actor_slack_user_id="U1"
    )
    TransitionService().apply(
        session, task=task, new_status=TaskStatus.done, actor_slack_user_id="U1"
    )
    hist = session.query(TaskStatusHistory).filter_by(task_id=task.id).order_by(
        TaskStatusHistory.id
    ).all()
    assert [h.to_status for h in hist] == [
        TaskStatus.in_progress,
        TaskStatus.review,
        TaskStatus.done,
    ]
    assert hist[0].from_status == TaskStatus.todo


def test_fr_cr4_transition_to_in_progress_sets_started_at(session):
    task = _make_task(session, status=TaskStatus.todo)
    TransitionService().apply(session, task=task, new_status=TaskStatus.in_progress)
    assert task.started_at is not None


def test_fr_cr4_transition_to_done_sets_completed_at(session):
    task = _make_task(session, status=TaskStatus.review)
    TransitionService().apply(session, task=task, new_status=TaskStatus.done)
    assert task.completed_at is not None


def test_fr_cr4_started_at_is_not_overwritten_on_second_in_progress(session):
    task = _make_task(session, status=TaskStatus.todo)
    TransitionService().apply(session, task=task, new_status=TaskStatus.in_progress)
    first = task.started_at
    TransitionService().apply(session, task=task, new_status=TaskStatus.review)
    TransitionService().apply(session, task=task, new_status=TaskStatus.in_progress)
    assert task.started_at == first


# =========================================================================== #
# FR-CR-5: Auto-subscribe owner and source author on create
# =========================================================================== #


def test_fr_cr5_owner_auto_subscribed_on_create(session):
    draft, snap = _mk_draft(
        session,
        payload={"title": "t", "owner_user_id": "U-owner"},
    )
    t = create_task_from_draft(
        session,
        draft=draft,
        source={},
        context_snapshot_id=snap.id,
        fallback_author_slack_id="U-author",
    )
    subs = {s.slack_user_id for s in session.query(TaskSubscription).all()}
    assert "U-owner" in subs
    assert "U-author" in subs


def test_fr_cr5_author_and_owner_same_person_is_single_row(session):
    draft, snap = _mk_draft(session, payload={"title": "t", "owner_user_id": "U1"})
    create_task_from_draft(
        session,
        draft=draft,
        source={},
        context_snapshot_id=snap.id,
        fallback_author_slack_id="U1",
    )
    assert session.query(TaskSubscription).count() == 1


def test_fr_cr5_subscribe_is_idempotent(session):
    task = _make_task(session)
    subs = SubscriptionService()
    subs.subscribe(session, task=task, slack_user_id="U9")
    subs.subscribe(session, task=task, slack_user_id="U9")
    assert (
        session.query(TaskSubscription).filter_by(task_id=task.id, slack_user_id="U9").count()
        == 1
    )


def test_fr_cr5_unsubscribe_removes_row(session):
    task = _make_task(session)
    subs = SubscriptionService()
    subs.subscribe(session, task=task, slack_user_id="U9")
    assert subs.unsubscribe(session, task=task, slack_user_id="U9") is True
    assert subs.is_subscribed(session, task=task, slack_user_id="U9") is False


def test_fr_cr5_unsubscribe_missing_returns_false(session):
    task = _make_task(session)
    subs = SubscriptionService()
    assert subs.unsubscribe(session, task=task, slack_user_id="Uxxx") is False


def test_fr_cr5_list_subscribers_returns_all(session):
    task = _make_task(session)
    subs = SubscriptionService()
    for uid in ("U1", "U2", "U3"):
        subs.subscribe(session, task=task, slack_user_id=uid)
    assert set(subs.list_subscribers(session, task=task)) == {"U1", "U2", "U3"}


# =========================================================================== #
# FR-CR-4 / FR-CR-5: Notifications on status change
# =========================================================================== #


class _RecordingSender:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def post_message(self, **kw):
        self.messages.append(kw)
        return {"ok": True}


def test_fr_cr5_status_change_broadcasts_to_every_subscriber(session):
    task = _make_task(session, status=TaskStatus.todo, owner="U-owner")
    subs = SubscriptionService()
    for uid in ("U-owner", "U-follower", "U-other"):
        subs.subscribe(session, task=task, slack_user_id=uid)

    sender = _RecordingSender()
    notifier = NotificationService(sender=sender, subscriptions=subs)
    count = notifier.broadcast_status_change(
        session,
        task=task,
        from_status=TaskStatus.todo,
        to_status=TaskStatus.in_progress,
        actor_slack_user_id="U-owner",
    )
    assert count == 3
    channels = {m["channel"] for m in sender.messages}
    assert channels == {"U-owner", "U-follower", "U-other"}


def test_fr_cr5_broadcast_single_failing_user_does_not_abort(session):
    task = _make_task(session)
    subs = SubscriptionService()
    for uid in ("U1", "U2", "U3"):
        subs.subscribe(session, task=task, slack_user_id=uid)

    class FlakySender:
        def __init__(self):
            self.ok_messages = []

        def post_message(self, **kw):
            if kw["channel"] == "U2":
                raise RuntimeError("boom")
            self.ok_messages.append(kw)
            return {"ok": True}

    sender = FlakySender()
    notifier = NotificationService(sender=sender, subscriptions=subs)
    count = notifier.broadcast_status_change(
        session,
        task=task,
        from_status=TaskStatus.todo,
        to_status=TaskStatus.in_progress,
        actor_slack_user_id="U1",
    )
    assert count == 2
    assert {m["channel"] for m in sender.ok_messages} == {"U1", "U3"}


def test_fr_cr5_deadline_approaching_notification(session):
    task = _make_task(session, due=date.today() + timedelta(days=1))
    subs = SubscriptionService()
    subs.subscribe(session, task=task, slack_user_id="U-owner")
    sender = _RecordingSender()
    NotificationService(sender=sender, subscriptions=subs).notify_deadline_approaching(
        session, task=task
    )
    assert sender.messages
    assert "due" in sender.messages[0]["text"].lower()


def test_fr_cr5_overdue_notification_text(session):
    task = _make_task(session, due=date.today() - timedelta(days=2))
    subs = SubscriptionService()
    subs.subscribe(session, task=task, slack_user_id="U-owner")
    sender = _RecordingSender()
    NotificationService(sender=sender, subscriptions=subs).notify_overdue(session, task=task)
    assert "overdue" in sender.messages[0]["text"].lower()


# =========================================================================== #
# Task card block structure
# =========================================================================== #


def test_task_card_shows_start_work_only_for_owner(session):
    task = _make_task(session, status=TaskStatus.todo, owner="U-owner")
    blocks_owner = bk.task_card(task=task, viewer_slack_user_id="U-owner")
    ids_owner = [
        el["action_id"]
        for b in blocks_owner
        if b["type"] == "actions"
        for el in b["elements"]
    ]
    assert bk.ACTION_START_WORK in ids_owner

    blocks_other = bk.task_card(task=task, viewer_slack_user_id="U-other")
    ids_other = [
        el["action_id"]
        for b in blocks_other
        if b["type"] == "actions"
        for el in b["elements"]
    ]
    assert bk.ACTION_START_WORK not in ids_other


def test_task_card_in_progress_exposes_only_done(session):
    # Product decision: the Review state is still in the model (some legacy
    # rows may sit there) but the UI no longer offers "Submit for review".
    # From in_progress the owner goes straight to Done.
    task = _make_task(session, status=TaskStatus.in_progress, owner="U-owner")
    blocks = bk.task_card(task=task, viewer_slack_user_id="U-owner")
    ids = [
        el["action_id"] for b in blocks if b["type"] == "actions" for el in b["elements"]
    ]
    assert bk.ACTION_MARK_DONE in ids
    assert bk.ACTION_SUBMIT_REVIEW not in ids


def test_task_card_review_exposes_only_done(session):
    task = _make_task(session, status=TaskStatus.review, owner="U-owner")
    blocks = bk.task_card(task=task, viewer_slack_user_id="U-owner")
    ids = [
        el["action_id"] for b in blocks if b["type"] == "actions" for el in b["elements"]
    ]
    assert bk.ACTION_MARK_DONE in ids
    assert bk.ACTION_START_WORK not in ids


def test_task_card_done_has_no_transition_buttons(session):
    task = _make_task(session, status=TaskStatus.done, owner="U-owner")
    blocks = bk.task_card(task=task, viewer_slack_user_id="U-owner")
    ids = [
        el["action_id"] for b in blocks if b["type"] == "actions" for el in b["elements"]
    ]
    # No lifecycle buttons on done
    for lifecycle in (
        bk.ACTION_START_WORK,
        bk.ACTION_SUBMIT_REVIEW,
        bk.ACTION_MARK_DONE,
    ):
        assert lifecycle not in ids


def test_task_card_toggles_subscribe_label(session):
    task = _make_task(session, status=TaskStatus.todo, owner="U-owner")
    subs_blocks = bk.task_card(task=task, viewer_slack_user_id="U-other", is_subscribed=True)
    ids = [
        el["action_id"] for b in subs_blocks if b["type"] == "actions" for el in b["elements"]
    ]
    assert bk.ACTION_UNSUBSCRIBE in ids

    unsubs_blocks = bk.task_card(task=task, viewer_slack_user_id="U-other", is_subscribed=False)
    ids = [
        el["action_id"] for b in unsubs_blocks if b["type"] == "actions" for el in b["elements"]
    ]
    assert bk.ACTION_SUBSCRIBE in ids


def test_task_card_hides_subscribe_for_owner(session):
    # Owners are implicitly subscribed — the toggle would be redundant.
    task = _make_task(session, status=TaskStatus.todo, owner="U-owner")
    blocks = bk.task_card(task=task, viewer_slack_user_id="U-owner")
    ids = [
        el["action_id"] for b in blocks if b["type"] == "actions" for el in b["elements"]
    ]
    assert bk.ACTION_SUBSCRIBE not in ids
    assert bk.ACTION_UNSUBSCRIBE not in ids


def test_task_card_does_not_include_open_source_button(session):
    """The card is posted in the source thread, so the back-link would be
    redundant. We deliberately omit it — see CR feedback 2026-04-23."""
    task = _make_task(session, status=TaskStatus.todo, owner="U-owner")
    task.source_permalink = "https://slack.com/archives/C1/p1"
    blocks = bk.task_card(task=task, viewer_slack_user_id="U-owner")
    ids = [
        el["action_id"]
        for b in blocks
        if b["type"] == "actions"
        for el in b["elements"]
    ]
    assert bk.ACTION_OPEN_SOURCE not in ids


def test_task_card_includes_show_context_when_snapshot_set(session):
    task = _make_task(session, status=TaskStatus.todo, owner="U-owner")
    task.context_snapshot_id = 42
    blocks = bk.task_card(task=task, viewer_slack_user_id="U-owner")
    ids = [
        el["action_id"] for b in blocks if b["type"] == "actions" for el in b["elements"]
    ]
    assert bk.ACTION_SHOW_CONTEXT in ids


def test_task_card_does_not_include_show_context_without_snapshot(session):
    task = _make_task(session, status=TaskStatus.todo, owner="U-owner")
    blocks = bk.task_card(task=task, viewer_slack_user_id="U-owner")
    ids = [
        el["action_id"] for b in blocks if b["type"] == "actions" for el in b["elements"]
    ]
    assert bk.ACTION_SHOW_CONTEXT not in ids
