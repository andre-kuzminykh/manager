"""Dedup gate for newly-proposed tasks (FR-CR-XX).

Every fresh `TaskDraft` from the ingest pipeline gets compared
against the last 20 open tasks via the LLM backend. When the LLM
says «duplicate», the candidate is skipped before a draft / widget
is dispatched.
"""
from __future__ import annotations

from datetime import date

from app.models import Task, TaskPriority, TaskSourceKind, TaskStatus
from app.services.task_dedup import DedupResult, check_duplicate


class _FakeBackend:
    """Minimal LLMBackend stub that returns a preset payload and
    records the user-prompt it was called with."""

    def __init__(self, payload):
        self.payload = payload
        self.last_user_prompt = None
        self.calls = 0

    def call_tool(self, **kw):
        self.calls += 1
        self.last_user_prompt = kw.get("user_prompt")
        return self.payload


def _mk(session, **kw) -> int:
    base = dict(
        title="t",
        priority=TaskPriority.medium,
        status=TaskStatus.todo,
        owner_user_id="11111",
        source_kind=TaskSourceKind.telegram,
    )
    base.update(kw)
    t = Task(**base)
    session.add(t)
    session.flush()
    return t.id


def test_dedup_prompt_pins_transliteration_rule():
    """FR-CR-05-92 — operator regression: «Предложить слоты для
    созвона с James Morgon» and «Предложить слоты Джеймсу
    Моргану» landed as TWO tasks. Same person, just one in
    English transliteration. Prompt must teach name-variant
    matching."""
    from app.services.task_dedup import _SYSTEM_PROMPT

    blob = _SYSTEM_PROMPT
    assert "TRANSLITERATION" in blob
    assert "FR-CR-05-92" in blob
    # Both regression spellings pinned.
    assert "James Morgon" in blob
    assert "Джеймсу Моргану" in blob
    # Other paired examples to anchor the rule.
    for fragment in ("Olayan", "Олаян", "Артем", "Артём", "Artem"):
        assert fragment in blob, f"name-variant {fragment!r} should be pinned"
    # Diminutives covered.
    assert "Petya" in blob or "Петя" in blob


def test_dedup_returns_not_duplicate_when_no_recent_tasks(session):
    backend = _FakeBackend({"is_duplicate": True})
    out = check_duplicate(
        session,
        candidate={"title": "новая задача"},
        llm_backend=backend,
    )
    # Empty lookback short-circuits — no LLM call is even made.
    assert out.is_duplicate is False
    assert backend.calls == 0


def test_dedup_returns_not_duplicate_when_no_backend(session):
    _mk(session, title="существующая")
    out = check_duplicate(
        session,
        candidate={"title": "новая"},
        llm_backend=None,
    )
    assert out.is_duplicate is False


def test_dedup_returns_duplicate_when_llm_says_so(session):
    existing = _mk(session, title="подготовить отчёт по продажам")
    backend = _FakeBackend(
        {
            "is_duplicate": True,
            "duplicate_of_task_id": existing,
            "reason": "same deliverable",
        }
    )
    out = check_duplicate(
        session,
        candidate={"title": "сделать отчёт по продажам"},
        llm_backend=backend,
    )
    assert out.is_duplicate is True
    assert out.duplicate_of_task_id == existing
    # The prompt must contain both the candidate title and the
    # existing one — without that the LLM has nothing to compare.
    assert "подготовить отчёт по продажам" in backend.last_user_prompt
    assert "сделать отчёт по продажам" in backend.last_user_prompt


def test_dedup_drops_invented_task_id(session):
    """The LLM occasionally hallucinates a task id outside our
    lookback — keep `is_duplicate=true` if the model claimed it,
    but null the id so the caller doesn't dereference garbage."""
    _mk(session, title="настоящая")
    backend = _FakeBackend(
        {
            "is_duplicate": True,
            "duplicate_of_task_id": 9999,  # not in the seeded lookback
        }
    )
    out = check_duplicate(
        session,
        candidate={"title": "новая"},
        llm_backend=backend,
    )
    assert out.is_duplicate is True
    assert out.duplicate_of_task_id is None


def test_dedup_returns_not_duplicate_when_llm_says_no(session):
    _mk(session, title="отчёт по продажам")
    backend = _FakeBackend({"is_duplicate": False})
    out = check_duplicate(
        session,
        candidate={"title": "позвонить клиенту"},
        llm_backend=backend,
    )
    assert out.is_duplicate is False


def test_dedup_swallows_llm_failure(session):
    """An LLM-side error (network, malformed response) must not
    abort the caller — the dedup gate falls open."""
    _mk(session, title="существующая")

    class _Boom:
        def call_tool(self, **kw):
            raise RuntimeError("network down")

    out = check_duplicate(
        session,
        candidate={"title": "новая"},
        llm_backend=_Boom(),
    )
    assert out.is_duplicate is False


def test_dedup_lookback_is_open_tasks_only(session):
    """Done / soft-deleted tasks aren't part of the lookback —
    they shouldn't suppress a fresh duplicate of completed work.
    With ONLY done/deleted tasks in the DB the lookback is empty,
    the LLM call short-circuits, and the candidate is allowed."""
    from datetime import datetime, timezone

    _mk(session, title="закрытая", status=TaskStatus.done)
    deleted_id = _mk(session, title="удалённая")
    deleted = session.get(Task, deleted_id)
    deleted.deleted_at = datetime.now(timezone.utc)
    session.flush()

    backend = _FakeBackend({"is_duplicate": True})  # would say dup
    out = check_duplicate(
        session,
        candidate={"title": "новая"},
        llm_backend=backend,
    )
    # No open tasks in lookback ⇒ no LLM call ⇒ not a duplicate.
    assert out.is_duplicate is False
    assert backend.calls == 0
    assert backend.last_user_prompt is None


def test_dedup_lookback_includes_only_open_existing_tasks(session):
    """Mixed seed: one open task and one done task. The prompt the
    LLM sees must mention only the open one."""
    open_id = _mk(session, title="открытая")
    _mk(session, title="закрытая", status=TaskStatus.done)

    backend = _FakeBackend({"is_duplicate": False})
    check_duplicate(
        session,
        candidate={"title": "новая"},
        llm_backend=backend,
    )
    assert backend.last_user_prompt is not None
    assert "открытая" in backend.last_user_prompt
    assert "закрытая" not in backend.last_user_prompt
    # T#-prefix marks this is a saved task (vs D#-prefix for drafts).
    assert f"T#{open_id}" in backend.last_user_prompt


def test_dedup_lookback_includes_open_action_drafts(session):
    """FR-CR-05-13 — sibling-draft dedup: when an earlier prepare_
    drafts call in the same batch left a proposed `ActionDraft`,
    the next candidate must see it in the lookback. Without this
    we'd ship two widgets («добавить Юру» × 2) for the same work
    in a single migration run."""
    from app.models import (
        ActionDraft,
        ActionDraftState,
        ContextSnapshot,
        IntentInference,
    )
    from app.models.intent import IntentType as IE

    snap = ContextSnapshot(
        conversation_id="C",
        source_ts="1",
        thread_ts=None,
        source_message={"ts": "1", "text": "x", "user": "U1"},
        history_before=[],
        thread_messages=[],
    )
    session.add(snap)
    session.flush()
    inf = IntentInference(
        context_snapshot_id=snap.id,
        intent=IE.create_task,
        confidence=0.9,
        invocation_type="passive",
    )
    session.add(inf)
    session.flush()
    draft = ActionDraft(
        inference_id=inf.id,
        intent=IE.create_task,
        state=ActionDraftState.proposed,
        payload={"title": "добавить Юру в участников"},
    )
    session.add(draft)
    session.flush()

    backend = _FakeBackend(
        {
            "is_duplicate": True,
            "duplicate_of_task_id": draft.id,
            "reason": "duplicate of pending draft",
        }
    )
    out = check_duplicate(
        session,
        candidate={"title": "добавить Юру"},
        llm_backend=backend,
    )
    assert out.is_duplicate is True
    assert out.duplicate_of_task_id == draft.id
    # The prompt must surface the sibling draft with a D#-prefix
    # so the LLM can address it distinctly from saved tasks.
    assert "D#" in backend.last_user_prompt
    assert "добавить Юру в участников" in backend.last_user_prompt


def test_dedup_invented_id_dropped_when_drafts_in_lookback(session):
    """The hallucination guard must work for both saved-task ids
    and draft ids. An LLM-invented id outside the union must be
    nulled."""
    open_task_id = _mk(session, title="real task")
    backend = _FakeBackend(
        {
            "is_duplicate": True,
            "duplicate_of_task_id": 99999,  # not in DB or drafts
        }
    )
    out = check_duplicate(
        session,
        candidate={"title": "x"},
        llm_backend=backend,
    )
    assert out.is_duplicate is True
    assert out.duplicate_of_task_id is None


def test_dedup_prompt_pins_different_recipient_rule():
    """FR-CR-05-78 — operator: «подготовить отчёт Ирине
    послезавтра» got killed as duplicate of «подготовить
    отчёт Артёму завтра». Different recipient + different
    deadline = different work, never duplicates. The prompt
    now spells this out explicitly with the exact regression
    case as a worked example."""
    from app.services.task_dedup import _SYSTEM_PROMPT

    blob = _SYSTEM_PROMPT
    # Default is «not duplicate».
    assert "DEFAULT TO" in blob and "false" in blob.lower()
    # Different recipient = not duplicate.
    assert "DIFFERENT RECIPIENT" in blob or "different recipient" in blob.lower()
    # The exact failure mode is pinned.
    assert "Ирине" in blob and "Артёму" in blob
