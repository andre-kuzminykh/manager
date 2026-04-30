"""FR-CR-05-61 — Google Tasks pull-side sync.

Covers:
- DB row's title / notes / due / status updated when the
  matching Google Tasks API row carries new values.
- `status=completed` in the API → DB Task → done + status
  history row.
- Tasks present in DB (with `google_tasks_id`) but absent
  from the API list → soft-deleted (`deleted_at = now`).
- Tasks the API knows about but DB doesn't (created in
  Google Tasks UI directly) — silently skipped, not
  hallucinated into DB rows.
- Refresh callback fired once per applied change.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.models import (
    GoogleTasksSync,
    SyncStatus,
    Task,
    TaskStatus,
    TaskStatusHistory,
)
from app.models.task import TaskPriority
from app.sync.tasks_pull import GoogleTasksPullService


def _stub_service(api_tasks: list[dict]) -> GoogleTasksPullService:
    """Build a service whose `_list_all` returns the canned list
    without touching real Google Tasks API."""
    svc = GoogleTasksPullService.__new__(GoogleTasksPullService)
    svc._service = MagicMock()
    svc._tasklist_id = "tasklist-1"
    svc._list_all = lambda: list(api_tasks)  # type: ignore[method-assign]
    return svc


def _mk_task(s, **kw) -> Task:
    base = dict(
        title="t",
        status=TaskStatus.todo,
        owner_user_id="111",
        priority=TaskPriority.medium,
        is_current_week=True,
    )
    base.update(kw)
    t = Task(**base)
    s.add(t)
    s.flush()
    return t


def _mk_sync_row(s, *, task_id: int, google_task_id: str, tasklist_id: str = "tasklist-1") -> GoogleTasksSync:
    row = GoogleTasksSync(
        task_id=task_id,
        tasklist_id=tasklist_id,
        google_task_id=google_task_id,
        status=SyncStatus.success,
    )
    s.add(row)
    s.flush()
    return row


# --------------------------------------------------------------------------- #
# Updates
# --------------------------------------------------------------------------- #


def test_pull_updates_title_and_notes_from_api(session):
    t = _mk_task(session, title="old title", description="old desc")
    _mk_sync_row(session, task_id=t.id, google_task_id="gid-1")
    session.commit()

    svc = _stub_service([
        {
            "id": "gid-1",
            "title": "edited title",
            "notes": "edited description",
            "status": "needsAction",
        }
    ])
    report = svc.pull(session)
    session.commit()

    assert report.updated == 1
    refreshed = session.get(Task, t.id)
    assert refreshed.title == "edited title"
    assert refreshed.description == "edited description"


def test_pull_propagates_status_completed_to_done_with_history(session):
    """Operator checks the box in Google Tasks → API status
    becomes `completed` → DB task transitions to `done`,
    `completed_at` set, status_history row written."""
    t = _mk_task(session, status=TaskStatus.in_progress)
    _mk_sync_row(session, task_id=t.id, google_task_id="gid-2")
    session.commit()

    svc = _stub_service([
        {
            "id": "gid-2",
            "title": t.title,
            "status": "completed",
        }
    ])
    report = svc.pull(session)
    session.commit()

    assert report.status_changed == 1
    refreshed = session.get(Task, t.id)
    assert refreshed.status == TaskStatus.done
    assert refreshed.completed_at is not None
    history = (
        session.query(TaskStatusHistory)
        .filter(TaskStatusHistory.task_id == t.id)
        .order_by(TaskStatusHistory.id.desc())
        .first()
    )
    assert history is not None
    assert history.from_status == TaskStatus.in_progress
    assert history.to_status == TaskStatus.done
    assert history.reason == "google_tasks_pull"


def test_pull_uncheck_done_in_api_returns_to_todo(session):
    """Operator unchecks the box in Google Tasks → API
    `status=needsAction` while DB has `done` → DB demotes to
    `todo` and clears `completed_at`."""
    t = _mk_task(session, status=TaskStatus.done, completed_at=datetime.now(timezone.utc))
    _mk_sync_row(session, task_id=t.id, google_task_id="gid-3")
    session.commit()

    svc = _stub_service([
        {
            "id": "gid-3",
            "title": t.title,
            "status": "needsAction",
        }
    ])
    svc.pull(session)
    session.commit()
    refreshed = session.get(Task, t.id)
    assert refreshed.status == TaskStatus.todo
    assert refreshed.completed_at is None


def test_pull_due_date_normalised_from_rfc3339(session):
    t = _mk_task(session, due_date=None)
    _mk_sync_row(session, task_id=t.id, google_task_id="gid-4")
    session.commit()

    svc = _stub_service([
        {
            "id": "gid-4",
            "title": t.title,
            "due": "2026-04-30T00:00:00.000Z",
            "status": "needsAction",
        }
    ])
    svc.pull(session)
    session.commit()
    refreshed = session.get(Task, t.id)
    assert refreshed.due_date == date(2026, 4, 30)


# --------------------------------------------------------------------------- #
# Deletes
# --------------------------------------------------------------------------- #


def test_pull_soft_deletes_tasks_missing_from_api_list(session):
    """A DB task with `google_tasks_id` set but NOT returned in
    the API list = operator deleted it in Google Tasks. Mark
    `deleted_at = now` so digests stop including it; write a
    cancellation history row."""
    t_kept = _mk_task(session, title="still here")
    t_gone = _mk_task(session, title="gone in google tasks")
    _mk_sync_row(session, task_id=t_kept.id, google_task_id="gid-K")
    _mk_sync_row(session, task_id=t_gone.id, google_task_id="gid-G")
    session.commit()

    svc = _stub_service([
        {"id": "gid-K", "title": "still here", "status": "needsAction"},
        # gid-G missing → operator deleted
    ])
    report = svc.pull(session)
    session.commit()

    assert report.deleted == 1
    assert session.get(Task, t_kept.id).deleted_at is None
    gone = session.get(Task, t_gone.id)
    assert gone.deleted_at is not None
    # Cancellation reason recorded.
    history = (
        session.query(TaskStatusHistory)
        .filter(
            TaskStatusHistory.task_id == t_gone.id,
            TaskStatusHistory.reason == "google_tasks_pull_deleted",
        )
        .first()
    )
    assert history is not None


def test_pull_does_not_redelete_already_soft_deleted(session):
    """`deleted_at` already set → don't record a duplicate
    history row. The pull stays idempotent on repeat calls."""
    t = _mk_task(session, deleted_at=datetime.now(timezone.utc))
    _mk_sync_row(session, task_id=t.id, google_task_id="gid-X")
    session.commit()

    svc = _stub_service([])  # gid-X absent
    report = svc.pull(session)
    session.commit()
    assert report.deleted == 0


# --------------------------------------------------------------------------- #
# Skipping rows we don't own
# --------------------------------------------------------------------------- #


def test_pull_skips_api_tasks_without_db_row(session):
    """Tasks the API knows about but our DB doesn't (operator
    created them straight in Google Tasks UI) are silently
    skipped — no hallucinated DB rows. Out of scope for v1."""
    svc = _stub_service([
        {
            "id": "gid-foreign",
            "title": "created in google tasks ui",
            "status": "needsAction",
        }
    ])
    report = svc.pull(session)
    session.commit()
    assert report.updated == 0
    assert report.deleted == 0
    assert session.query(Task).count() == 0


def test_pull_skips_when_db_task_already_soft_deleted(session):
    """A DB row that's already deleted_at-set + still in API
    (operator deletes in TG, not yet in Google Tasks): we don't
    revive it. Update is silently skipped."""
    t = _mk_task(session, deleted_at=datetime.now(timezone.utc))
    _mk_sync_row(session, task_id=t.id, google_task_id="gid-Z")
    session.commit()
    svc = _stub_service([
        {"id": "gid-Z", "title": "edited", "status": "needsAction"},
    ])
    report = svc.pull(session)
    session.commit()
    assert report.updated == 0
    refreshed = session.get(Task, t.id)
    assert refreshed.title == "t"  # untouched


# --------------------------------------------------------------------------- #
# Refresh callback
# --------------------------------------------------------------------------- #


def test_pull_fires_refresh_callback_per_applied_change(session):
    t1 = _mk_task(session, title="a")
    t2 = _mk_task(session, title="b")
    _mk_sync_row(session, task_id=t1.id, google_task_id="g1")
    _mk_sync_row(session, task_id=t2.id, google_task_id="g2")
    session.commit()

    svc = _stub_service([
        {"id": "g1", "title": "edited a", "status": "needsAction"},
        {"id": "g2", "title": "b", "status": "needsAction"},  # unchanged
    ])
    refreshed: list[int] = []
    svc.pull(session, refresh_card=lambda task: refreshed.append(task.id))
    session.commit()
    assert refreshed == [t1.id]
