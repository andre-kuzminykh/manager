"""Requirement coverage: FR-CR-03-5 (editable tasks with history),
NFR-CR-03-2 (every admin action writes an audit row),
NFR-CR-03-5 (non-admin click rejected with ephemeral).

Complements test_cr03_admin_review by exercising the edge cases:
non-admin clicks (ephemeral lock), missing task row, admin edit modal
open/submit with diff tracking."""
from __future__ import annotations

import json

from app.models import AuditLog, Task
from app.models.task import TaskPriority, TaskStatus
from app.slack_bot.handlers.admin_review import (
    handle_admin_confirm,
    handle_admin_edit_open,
    handle_admin_edit_submit,
    handle_admin_reject,
)


class _Sender:
    def __init__(self):
        self.posts: list[dict] = []
        self.updates: list[dict] = []
        self.ephemerals: list[dict] = []

    def post_message(self, **kw):
        self.posts.append(kw)
        return {"ok": True, "ts": f"{len(self.posts)}.0"}

    def post_ephemeral(self, **kw):
        self.ephemerals.append(kw)
        return {"ok": True}

    def update_message(self, **kw):
        self.updates.append(kw)
        return {"ok": True}


class _Client:
    def __init__(self):
        self.opened: list[dict] = []

    def views_open(self, *, trigger_id, view):  # noqa: N802
        self.opened.append({"trigger_id": trigger_id, "view": view})


def _admin_body(task_id: int, *, user: str, channel: str = "C1"):
    return {
        "actions": [{"value": str(task_id)}],
        "user": {"id": user},
        "channel": {"id": channel},
        "message": {"ts": "100.0"},
        "trigger_id": "trig-1",
    }


# --------------------------------------------------------------------------- #
# Non-admin gate
# --------------------------------------------------------------------------- #


def test_confirm_rejects_non_admin_with_ephemeral(
    patched_session_scope, SessionFactory, ack, monkeypatch
):
    monkeypatch.setenv("ADMIN_SLACK_USER_IDS", "U-admin")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        with SessionFactory() as s:
            t = Task(title="t", status=TaskStatus.todo, owner_user_id="U-owner")
            s.add(t)
            s.commit()
            tid = t.id

        sender = _Sender()
        handle_admin_confirm(body=_admin_body(tid, user="U-other"), sender=sender, ack=ack)
        # No audit row written, ephemeral lock fired.
        with SessionFactory() as s:
            assert s.query(AuditLog).filter(AuditLog.category == "admin_review").count() == 0
        assert sender.ephemerals
        assert sender.ephemerals[0]["user"] == "U-other"
        assert "админ" in sender.ephemerals[0]["text"].lower()
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_reject_rejects_non_admin(
    patched_session_scope, SessionFactory, ack, monkeypatch
):
    monkeypatch.setenv("ADMIN_SLACK_USER_IDS", "U-admin")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        with SessionFactory() as s:
            t = Task(title="t", status=TaskStatus.todo, owner_user_id="U-owner")
            s.add(t)
            s.commit()
            tid = t.id

        sender = _Sender()
        handle_admin_reject(body=_admin_body(tid, user="U-other"), sender=sender, ack=ack)
        with SessionFactory() as s:
            # Task still present, no audit.
            assert s.query(Task).count() == 1
            assert s.query(AuditLog).filter(AuditLog.category == "admin_review").count() == 0
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_edit_open_rejects_non_admin(
    patched_session_scope, SessionFactory, ack, monkeypatch
):
    monkeypatch.setenv("ADMIN_SLACK_USER_IDS", "U-admin")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        with SessionFactory() as s:
            t = Task(title="t", status=TaskStatus.todo, owner_user_id="U-owner")
            s.add(t)
            s.commit()
            tid = t.id

        sender = _Sender()
        client = _Client()
        handle_admin_edit_open(
            body=_admin_body(tid, user="U-other"),
            client=client,
            sender=sender,
            ack=ack,
        )
        assert client.opened == []
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Missing task rows
# --------------------------------------------------------------------------- #


def test_confirm_ignores_when_task_id_missing(patched_session_scope, ack):
    sender = _Sender()
    handle_admin_confirm(
        body={"actions": [{"value": ""}], "user": {"id": "U-admin"}},
        sender=sender,
        ack=ack,
    )
    # Nothing posted.
    assert sender.posts == []
    assert sender.ephemerals == []


def test_reject_on_unknown_task_replaces_card(
    patched_session_scope, SessionFactory, ack, monkeypatch
):
    monkeypatch.setenv("ADMIN_SLACK_USER_IDS", "U-admin")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        sender = _Sender()
        handle_admin_reject(
            body=_admin_body(9999, user="U-admin"), sender=sender, ack=ack
        )
        # Card-replacement update was attempted even though the task is gone.
        assert sender.updates
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Admin edit modal: open + submit
# --------------------------------------------------------------------------- #


def test_admin_edit_open_populates_modal(
    patched_session_scope, SessionFactory, ack, monkeypatch
):
    from datetime import date

    monkeypatch.setenv("ADMIN_SLACK_USER_IDS", "U-admin")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        with SessionFactory() as s:
            t = Task(
                title="собрать отчёт",
                status=TaskStatus.todo,
                owner_user_id="U-owner",
                priority=TaskPriority.high,
                due_date=date(2026, 5, 1),
            )
            s.add(t)
            s.commit()
            tid = t.id

        client = _Client()
        sender = _Sender()
        handle_admin_edit_open(
            body=_admin_body(tid, user="U-admin"),
            client=client,
            sender=sender,
            ack=ack,
        )
        assert client.opened
        view = client.opened[0]["view"]
        pm = json.loads(view.get("private_metadata") or "{}")
        assert pm.get("edit_task_id") == tid
        # Admin-review message metadata is stored for the submit-handler
        # to update the review card later.
        assert pm.get("admin_review_msg") == {"channel": "C1", "ts": "100.0"}
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_admin_edit_submit_writes_diff_audit(
    patched_session_scope, SessionFactory, ack
):
    from datetime import date

    from app.slack_bot import blocks as bk

    with SessionFactory() as s:
        t = Task(
            title="старый заголовок",
            description="старое описание",
            status=TaskStatus.todo,
            owner_user_id="U-owner",
            priority=TaskPriority.medium,
            due_date=date(2026, 5, 1),
        )
        s.add(t)
        s.commit()
        tid = t.id

    view = {
        "callback_id": bk.MODAL_CALLBACK_ADMIN_EDIT,
        "private_metadata": json.dumps({"edit_task_id": tid}),
        "state": {
            "values": {
                bk.BLOCK_TITLE: {bk.INPUT_TITLE: {"value": "новый заголовок"}},
                bk.BLOCK_DESCRIPTION: {bk.INPUT_DESCRIPTION: {"value": "новое описание"}},
                bk.BLOCK_OWNER: {
                    bk.INPUT_OWNER: {
                        "selected_option": {
                            "value": "U-ivan",
                            "text": {"type": "plain_text", "text": "Ivan"},
                        }
                    }
                },
                bk.BLOCK_PRIORITY: {
                    bk.INPUT_PRIORITY: {"selected_option": {"value": "high"}}
                },
                bk.BLOCK_DUE: {bk.INPUT_DUE: {"selected_date": "2026-05-15"}},
                bk.BLOCK_EFFORT: {bk.INPUT_EFFORT: {"value": ""}},
            }
        },
    }
    sender = _Sender()
    handle_admin_edit_submit(
        body={"user": {"id": "U-admin"}}, view=view, sender=sender, ack=ack
    )

    with SessionFactory() as s:
        task = s.get(Task, tid)
        assert task.title == "новый заголовок"
        assert task.description == "новое описание"
        assert task.owner_user_id == "U-ivan"
        assert task.priority.value == "high"
        assert task.due_date == date(2026, 5, 15)

        audit = (
            s.query(AuditLog)
            .filter(AuditLog.category == "admin_review", AuditLog.action == "task_edited")
            .one()
        )
        diff = audit.payload["diff"]
        assert diff["title"] == ["старый заголовок", "новый заголовок"]
        assert diff["priority"] == ["medium", "high"]
        assert diff["due_date"] == ["2026-05-01", "2026-05-15"]


def test_admin_edit_submit_rejects_empty_title(patched_session_scope, ack):
    from app.slack_bot import blocks as bk

    captured: list = []

    def _ack(**kw):
        captured.append(kw)

    view = {
        "callback_id": bk.MODAL_CALLBACK_ADMIN_EDIT,
        "private_metadata": "{}",
        "state": {
            "values": {
                bk.BLOCK_TITLE: {bk.INPUT_TITLE: {"value": ""}},
                bk.BLOCK_DESCRIPTION: {bk.INPUT_DESCRIPTION: {"value": ""}},
                bk.BLOCK_OWNER: {bk.INPUT_OWNER: {"selected_user": ""}},
                bk.BLOCK_PRIORITY: {
                    bk.INPUT_PRIORITY: {"selected_option": {"value": "medium"}}
                },
                bk.BLOCK_DUE: {bk.INPUT_DUE: {"selected_date": None}},
                bk.BLOCK_EFFORT: {bk.INPUT_EFFORT: {"value": ""}},
            }
        },
    }
    handle_admin_edit_submit(
        body={"user": {"id": "U-admin"}},
        view=view,
        sender=_Sender(),
        ack=_ack,
    )
    assert captured
    assert captured[0]["response_action"] == "errors"
    assert bk.BLOCK_TITLE in captured[0]["errors"]
