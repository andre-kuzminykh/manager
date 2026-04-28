"""FR-CR-04-29 — reply-conversation flows for Mark done + Edit.

Covers:

- `prompt_done` returns the artifact prompt; raises NotAuthorised
  for strangers.
- `apply_done_artifact_reply` parses URL vs free-text, persists on
  the task, transitions to done.
- `prompt_edit` returns the help text with current values.
- `parse_edit_payload` handles the multi-line key=value reply,
  drops unknown keys, handles empty values (= clear field).
- `apply_edit_reply` actually flips the fields on the task.
- `PendingRegistry` register / take / TTL eviction.
- TG admin support — `is_admin` / `_ensure_can_edit` honours the
  TELEGRAM_ADMIN_USER_IDS env.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from app.models import Task, TaskPriority, TaskSourceKind, TaskStatus
from app.telegram_bot import handlers as h
from app.telegram_bot.pending import PendingQuestion, PendingRegistry


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
# Pending registry
# --------------------------------------------------------------------------- #


def test_pending_register_and_take_round_trip():
    reg = PendingRegistry()
    reg.register(
        action="artifact",
        task_id=42,
        chat_id=-100,
        user_id=1,
        prompt_message_id=99,
    )
    out = reg.take(chat_id=-100, user_id=1, reply_to_message_id=99)
    assert out is not None
    assert out.action == "artifact"
    assert out.task_id == 42
    # Second take is empty — register was consumed.
    assert reg.take(chat_id=-100, user_id=1, reply_to_message_id=99) is None


def test_pending_take_with_no_reply_to_returns_none():
    reg = PendingRegistry()
    reg.register(
        action="edit", task_id=1, chat_id=1, user_id=1, prompt_message_id=10
    )
    assert reg.take(chat_id=1, user_id=1, reply_to_message_id=None) is None


def test_pending_evict_expired_drops_old_entries():
    reg = PendingRegistry(ttl_seconds=0)  # instantly expired
    reg.register(
        action="artifact",
        task_id=1,
        chat_id=1,
        user_id=1,
        prompt_message_id=1,
    )
    # take returns None when expired
    assert reg.take(chat_id=1, user_id=1, reply_to_message_id=1) is None


def test_pending_take_doesnt_match_a_different_prompt_message():
    reg = PendingRegistry()
    reg.register(
        action="artifact",
        task_id=1,
        chat_id=1,
        user_id=1,
        prompt_message_id=10,
    )
    assert reg.take(chat_id=1, user_id=1, reply_to_message_id=11) is None


# --------------------------------------------------------------------------- #
# Mark done — prompt + apply
# --------------------------------------------------------------------------- #


def test_prompt_done_returns_text_for_owner(session):
    tid = _mk(session, owner_user_id="11", status=TaskStatus.in_progress)
    task, text = h.prompt_done(session, task_id=tid, actor="11")
    assert task.id == tid
    assert "Mark done" in text or "/skip" in text


def test_prompt_done_blocks_stranger(session):
    tid = _mk(session, owner_user_id="11")
    with pytest.raises(h.NotAuthorised):
        h.prompt_done(session, task_id=tid, actor="99")


def test_apply_done_skip_completes_without_artifact(session):
    tid = _mk(session, owner_user_id="11", status=TaskStatus.in_progress)
    task = h.apply_done_artifact_reply(
        session, task_id=tid, actor="11", reply_text="/skip"
    )
    assert task.status == TaskStatus.done
    assert task.completion_artifact is None
    assert task.completion_artifact_kind is None


def test_apply_done_url_artifact(session):
    tid = _mk(session, owner_user_id="11", status=TaskStatus.in_progress)
    task = h.apply_done_artifact_reply(
        session,
        task_id=tid,
        actor="11",
        reply_text="https://drive.example.com/file",
    )
    assert task.status == TaskStatus.done
    assert task.completion_artifact == "https://drive.example.com/file"
    assert task.completion_artifact_kind == "url"


def test_apply_done_text_artifact(session):
    tid = _mk(session, owner_user_id="11", status=TaskStatus.in_progress)
    task = h.apply_done_artifact_reply(
        session,
        task_id=tid,
        actor="11",
        reply_text="report sent to ops, screenshot attached",
    )
    assert task.status == TaskStatus.done
    assert task.completion_artifact_kind == "text"
    assert "report sent" in task.completion_artifact


# --------------------------------------------------------------------------- #
# Edit — prompt + parse + apply
# --------------------------------------------------------------------------- #


def test_prompt_edit_includes_current_values(session):
    tid = _mk(
        session,
        owner_user_id="11",
        title="Old title",
        priority=TaskPriority.medium,
        due_date=date(2026, 5, 1),
    )
    _, text = h.prompt_edit(session, task_id=tid, actor="11")
    assert "Old title" in text
    assert "medium" in text
    assert "2026-05-01" in text


def test_parse_edit_payload_handles_multiline_kv():
    out = h.parse_edit_payload(
        "title=New title\n"
        "priority=high\n"
        "due=2026-05-15\n"
        "rubbish=ignored\n"
    )
    assert out == {
        "title": "New title",
        "priority": "high",
        "due": "2026-05-15",
    }


def test_parse_edit_payload_empty_value_means_clear():
    out = h.parse_edit_payload("description=\ncategory=marketing\n")
    assert out["description"] == ""
    assert out["category"] == "marketing"


def test_apply_edit_changes_fields(session):
    tid = _mk(
        session,
        owner_user_id="11",
        title="Old",
        priority=TaskPriority.low,
        due_date=None,
    )
    task = h.apply_edit_reply(
        session,
        task_id=tid,
        actor="11",
        reply_text=(
            "title=New title\n"
            "priority=urgent\n"
            "due=2026-06-15\n"
            "due_time=14:00\n"
            "category=ops\n"
        ),
    )
    assert task.title == "New title"
    assert task.priority == TaskPriority.urgent
    assert task.due_date == date(2026, 6, 15)
    assert task.due_time == time(14, 0)
    assert task.category == "ops"


def test_apply_edit_clears_field_on_empty_value(session):
    tid = _mk(
        session,
        owner_user_id="11",
        category="oldcat",
        due_date=date(2026, 5, 1),
    )
    task = h.apply_edit_reply(
        session,
        task_id=tid,
        actor="11",
        reply_text="category=\ndue=\n",
    )
    assert task.category is None
    assert task.due_date is None


def test_apply_edit_invalid_priority_left_unchanged(session):
    tid = _mk(session, owner_user_id="11", priority=TaskPriority.medium)
    task = h.apply_edit_reply(
        session,
        task_id=tid,
        actor="11",
        reply_text="priority=critical\n",
    )
    assert task.priority == TaskPriority.medium  # invalid → unchanged


def test_apply_edit_blocks_stranger(session):
    tid = _mk(session, owner_user_id="11")
    with pytest.raises(h.NotAuthorised):
        h.apply_edit_reply(
            session, task_id=tid, actor="99", reply_text="title=x"
        )


def test_apply_edit_drops_owner_assumed_extra(session):
    tid = _mk(
        session,
        owner_user_id="11",
        extra={"owner_assumed": True},
    )
    task = h.apply_edit_reply(
        session, task_id=tid, actor="11", reply_text="title=Renamed"
    )
    assert (task.extra or {}).get("owner_assumed") is None


# --------------------------------------------------------------------------- #
# TG admins
# --------------------------------------------------------------------------- #


def test_admin_user_ids_parses_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "111, 222 ,  ,333")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        ids = h.admin_user_ids()
        assert ids == {"111", "222", "333"}
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_admin_can_edit_task_they_dont_own(session, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "777")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        tid = _mk(session, owner_user_id="11")
        task = session.get(Task, tid)
        # No raise — admin can edit anyone's task.
        h._ensure_can_edit(task, "777")
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_non_admin_non_owner_blocked(session, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "777")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        tid = _mk(session, owner_user_id="11")
        task = session.get(Task, tid)
        with pytest.raises(h.NotAuthorised):
            h._ensure_can_edit(task, "555")
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]
