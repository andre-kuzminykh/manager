"""Requirement coverage: FR-CR-03-8 (admin watch-list digest, morning
+ evening), NFR-CR-03-3 (digests idempotent per user+date).

CR-03 Phase E: admin evening digest + morning watch-list."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.models import AuditLog, Task, TaskStatus
from app.services import send_admin_evening_digest, send_admin_morning_watch


class _S:
    def __init__(self):
        self.posts: list[dict] = []

    def post_message(self, **kw):
        self.posts.append(kw)
        return {"ok": True, "ts": "0"}


def _task(session, **kw):
    t = Task(title=kw.pop("title", "t"), **kw)
    session.add(t)
    session.flush()
    return t


# --------------------------------------------------------------------------- #
# Evening digest
# --------------------------------------------------------------------------- #


def test_evening_skipped_when_no_admins_configured(session, monkeypatch):
    monkeypatch.setenv("ADMIN_SLACK_USER_IDS", "")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    sender = _S()
    report = send_admin_evening_digest(session, sender=sender, today=date(2026, 4, 23))
    assert report.recipients == 0
    assert sender.posts == []
    get_settings.cache_clear()  # type: ignore[attr-defined]


def test_evening_dms_every_admin(session, monkeypatch):
    monkeypatch.setenv("ADMIN_SLACK_USER_IDS", "U-a1,U-a2")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    _task(session, status=TaskStatus.todo, owner_user_id="U1", due_date=date(2026, 4, 24))
    sender = _S()
    report = send_admin_evening_digest(session, sender=sender, today=date(2026, 4, 23))
    assert report.recipients == 2
    assert {m["channel"] for m in sender.posts} == {"U-a1", "U-a2"}
    get_settings.cache_clear()  # type: ignore[attr-defined]


def test_evening_lists_tomorrow_tasks(session, monkeypatch):
    monkeypatch.setenv("ADMIN_SLACK_USER_IDS", "U-a")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    _task(
        session,
        title="deadline_tomorrow",
        status=TaskStatus.todo,
        owner_user_id="U1",
        due_date=date(2026, 4, 24),
    )
    _task(
        session,
        title="not_tomorrow",
        status=TaskStatus.todo,
        owner_user_id="U1",
        due_date=date(2026, 4, 30),
    )
    sender = _S()
    send_admin_evening_digest(session, sender=sender, today=date(2026, 4, 23))
    body = sender.posts[0]["blocks"][0]["text"]["text"]
    assert "deadline_tomorrow" in body
    assert "not_tomorrow" not in body
    get_settings.cache_clear()  # type: ignore[attr-defined]


def test_evening_flags_stale_in_progress(session, monkeypatch):
    monkeypatch.setenv("ADMIN_SLACK_USER_IDS", "U-a")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    t = _task(
        session,
        status=TaskStatus.in_progress,
        owner_user_id="U1",
        title="stale",
    )
    # started 5 days ago → stale
    t.started_at = datetime(2026, 4, 18, tzinfo=timezone.utc)
    session.flush()
    fresh = _task(
        session,
        status=TaskStatus.in_progress,
        owner_user_id="U2",
        title="fresh",
    )
    fresh.started_at = datetime(2026, 4, 22, tzinfo=timezone.utc)
    session.flush()

    sender = _S()
    send_admin_evening_digest(
        session, sender=sender, today=date(2026, 4, 23), stale_threshold_days=2
    )
    body = sender.posts[0]["blocks"][0]["text"]["text"]
    assert "stale" in body
    assert "fresh" not in body
    get_settings.cache_clear()  # type: ignore[attr-defined]


def test_evening_idempotent_same_day(session, monkeypatch):
    monkeypatch.setenv("ADMIN_SLACK_USER_IDS", "U-a")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    _task(session, status=TaskStatus.todo, owner_user_id="U1", due_date=date(2026, 4, 24))
    sender = _S()
    send_admin_evening_digest(session, sender=sender, today=date(2026, 4, 23))
    send_admin_evening_digest(session, sender=sender, today=date(2026, 4, 23))
    assert len(sender.posts) == 1
    get_settings.cache_clear()  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Morning watch-list
# --------------------------------------------------------------------------- #


def test_morning_watch_lists_in_progress_and_overdue(session, monkeypatch):
    monkeypatch.setenv("ADMIN_SLACK_USER_IDS", "U-a")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]

    today = date(2026, 4, 23)
    _task(session, status=TaskStatus.in_progress, owner_user_id="U1", title="doing")
    _task(
        session,
        status=TaskStatus.todo,
        owner_user_id="U3",
        title="late",
        due_date=today - timedelta(days=1),
    )
    _task(
        session,
        status=TaskStatus.backlog,
        owner_user_id="U4",
        title="future",
        due_date=today + timedelta(days=10),
    )

    sender = _S()
    send_admin_morning_watch(session, sender=sender, today=today)
    body = sender.posts[0]["blocks"][0]["text"]["text"]
    assert "doing" in body
    assert "late" in body
    assert "future" not in body
    get_settings.cache_clear()  # type: ignore[attr-defined]


def test_morning_watch_skipped_when_no_admins(session, monkeypatch):
    monkeypatch.setenv("ADMIN_SLACK_USER_IDS", "")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    _task(session, status=TaskStatus.in_progress, owner_user_id="U1")
    sender = _S()
    report = send_admin_morning_watch(session, sender=sender, today=date(2026, 4, 23))
    assert report.recipients == 0
    get_settings.cache_clear()  # type: ignore[attr-defined]


def test_morning_watch_idempotent_same_day(session, monkeypatch):
    monkeypatch.setenv("ADMIN_SLACK_USER_IDS", "U-a")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    _task(session, status=TaskStatus.in_progress, owner_user_id="U1")
    sender = _S()
    send_admin_morning_watch(session, sender=sender, today=date(2026, 4, 23))
    send_admin_morning_watch(session, sender=sender, today=date(2026, 4, 23))
    assert len(sender.posts) == 1
    get_settings.cache_clear()  # type: ignore[attr-defined]


def test_evening_audit_row_carries_counts(session, monkeypatch):
    monkeypatch.setenv("ADMIN_SLACK_USER_IDS", "U-a")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    _task(session, status=TaskStatus.todo, owner_user_id="U1", due_date=date(2026, 4, 24))
    send_admin_evening_digest(session, sender=_S(), today=date(2026, 4, 23))
    log = (
        session.query(AuditLog)
        .filter(AuditLog.category == "admin_digest")
        .one()
    )
    assert log.payload["tomorrow"] == 1
    get_settings.cache_clear()  # type: ignore[attr-defined]
