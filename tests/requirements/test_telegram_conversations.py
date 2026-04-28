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
# Edit — LLM-driven free-form parsing
# --------------------------------------------------------------------------- #


class _FakeBackend:
    """Minimal LLMBackend stub: returns whatever was preset for the
    next `call_tool` call, and records the prompt for assertions."""

    def __init__(self, payload):
        self.payload = payload
        self.last_user_prompt = None

    def call_tool(self, **kw):
        self.last_user_prompt = kw.get("user_prompt")
        return self.payload


def test_parse_edit_with_llm_explicit_kv_skips_llm(session):
    tid = _mk(session, owner_user_id="11", title="Old")
    task = session.get(Task, tid)
    backend = _FakeBackend(payload={"title": "Should not be used"})
    out = h.parse_edit_with_llm(
        task=task,
        reply_text="title=Renamed via kv\npriority=high",
        backend=backend,
    )
    # Pure key=value — LLM not consulted.
    assert backend.last_user_prompt is None
    assert out == {"title": "Renamed via kv", "priority": "high"}


def test_parse_edit_with_llm_freeform_calls_backend(session):
    tid = _mk(
        session, owner_user_id="11", title="Old", priority=TaskPriority.low
    )
    task = session.get(Task, tid)
    backend = _FakeBackend(
        payload={"priority": "high", "due": "2026-05-15"}
    )
    out = h.parse_edit_with_llm(
        task=task,
        reply_text="сделай высокий приоритет и срок 15 мая",
        backend=backend,
    )
    assert backend.last_user_prompt is not None
    # Current values appear in the prompt so the LLM has context.
    assert "Old" in backend.last_user_prompt
    assert out == {"priority": "high", "due": "2026-05-15"}


def test_parse_edit_with_llm_no_backend_falls_back_to_kv(session):
    tid = _mk(session, owner_user_id="11")
    task = session.get(Task, tid)
    out = h.parse_edit_with_llm(
        task=task,
        reply_text="just plain text, no kv",
        backend=None,
    )
    # No backend + no key=value → empty payload.
    assert out == {}


def test_apply_edit_reply_uses_llm_backend_when_provided(session):
    tid = _mk(
        session, owner_user_id="11", title="Old", priority=TaskPriority.low
    )
    backend = _FakeBackend(payload={"priority": "urgent"})
    task = h.apply_edit_reply(
        session,
        task_id=tid,
        actor="11",
        reply_text="сделай срочный приоритет",
        llm_backend=backend,
    )
    assert task.priority == TaskPriority.urgent
    assert backend.last_user_prompt is not None


def test_apply_edit_reply_ex_returns_empty_payload_when_llm_silent(session):
    """When the LLM extracts nothing actionable, the listener gets an
    empty payload back so it can post a hint instead of silently
    refreshing the card."""
    tid = _mk(session, owner_user_id="11", title="Old")
    backend = _FakeBackend(payload={})
    task, applied = h.apply_edit_reply_ex(
        session,
        task_id=tid,
        actor="11",
        reply_text="завтра",
        llm_backend=backend,
    )
    assert task is not None
    assert applied == {}


# --------------------------------------------------------------------------- #
# Edit-on-draft (FR-CR-04-32 ext)
# --------------------------------------------------------------------------- #


def _mk_draft(session, **kw):
    """Helper to seed a proposed ActionDraft for the tests below."""
    from app.models import ActionDraft, ActionDraftState, IntentInference
    from app.models.intent import IntentType as IT

    inference = IntentInference(
        intent=IT.create_task,
        confidence=0.9,
        invocation_type="passive",
    )
    session.add(inference)
    session.flush()
    payload = {
        "title": kw.get("title", "draft title"),
        "priority": kw.get("priority", "medium"),
        "due_date": kw.get("due_date"),
        "owner_user_id": kw.get("owner_user_id", "11"),
        "_widgets": [{"chat_id": 11, "message_id": 99}],
    }
    d = ActionDraft(
        inference_id=inference.id,
        intent=IT.create_task,
        state=ActionDraftState.proposed,
        payload=payload,
        created_by_slack_user_id=kw.get("author", "11"),
    )
    session.add(d)
    session.flush()
    return d.id


def test_prompt_edit_draft_lists_filled_and_missing(session):
    did = _mk_draft(session, title="Old", priority="low", due_date=None)
    _, text = h.prompt_edit_draft(session, draft_id=did, actor="11")
    assert "Edit draft" in text
    assert "Title" in text
    # Due not set → mentioned in the "Missing:" line.
    assert "Missing" in text
    assert "due" in text


def test_prompt_edit_draft_blocks_stranger(session):
    did = _mk_draft(session, author="11")
    with pytest.raises(h.NotAuthorised):
        h.prompt_edit_draft(session, draft_id=did, actor="99")


def test_apply_edit_draft_reply_updates_payload(session):
    did = _mk_draft(session, title="Old", priority="low")
    backend = _FakeBackend(payload={"priority": "urgent", "due": "2026-05-15"})
    draft, applied = h.apply_edit_draft_reply(
        session,
        draft_id=did,
        actor="11",
        reply_text="сделай срочный приоритет до 15 мая",
        llm_backend=backend,
    )
    assert draft is not None
    assert applied == {"priority": "urgent", "due": "2026-05-15"}
    # The draft.payload mutated in-place; widgets list survives so
    # the listener can still re-render every DM.
    assert draft.payload["priority"] == "urgent"
    assert draft.payload["due_date"] == "2026-05-15"
    assert draft.payload["_widgets"] == [{"chat_id": 11, "message_id": 99}]


def test_apply_edit_draft_reply_returns_empty_when_llm_silent(session):
    did = _mk_draft(session)
    backend = _FakeBackend(payload={})
    draft, applied = h.apply_edit_draft_reply(
        session,
        draft_id=did,
        actor="11",
        reply_text="hmm",
        llm_backend=backend,
    )
    assert draft is not None
    assert applied == {}


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
