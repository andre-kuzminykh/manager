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


def test_dedup_prompt_is_minimal_focused_classifier():
    """FR-CR-05-101 — operator: «нужно сравнить описание новой
    задачи с контекстом из 10 предыдущих задач и спросить это
    дублирует хоть что-то из этих задач? просто без множества
    усложнений». Final form: ≤700 chars, no synonym families,
    no worked examples, no rules — just a clean binary
    classifier framing."""
    from app.services.task_dedup import _SYSTEM_PROMPT

    blob = _SYSTEM_PROMPT
    flat = " ".join(blob.split())

    assert len(blob) <= 700
    # Compare descriptions, not just titles.
    assert "Compare descriptions" in blob or "compare descriptions" in flat.lower()
    # 10 items framing.
    assert "10 existing" in blob
    # Output schema.
    assert "is_duplicate" in blob and "duplicate_of_task_id" in blob


def test_normalize_title_for_match_collapses_whitespace_case_yo_e():
    """FR-CR-05-97 — exact-match normaliser handles common
    LLM-output variations: case, internal whitespace, leading
    punctuation, ё↔е."""
    from app.services.task_dedup import _normalize_title_for_match

    f = _normalize_title_for_match
    assert f("Подтвердить") == f("подтвердить")
    assert f("Подтвердить  встречу") == f("Подтвердить встречу")
    assert f(" Подтвердить ") == f("Подтвердить")
    assert f("Подтвердить.") == f("Подтвердить")
    # ё / е equivalence.
    assert f("Подтвердить тёщу") == f("Подтвердить тещу")


def test_dedup_dispatches_to_llm_with_full_descriptions(session):
    """FR-CR-05-100 — operator: «по описанию задачи надо».
    The LLM gets each existing item with its FULL description
    (≤1500 chars), not a 200-char snippet. Two drafts with
    same external entity but slightly differing description
    wordings («необходимо» vs «нужно») must be visible to the
    model in full so it can spot the overlap."""
    from app.services.task_dedup import check_duplicate

    existing = _mk(
        session,
        title="Взять обратную связь по PALADIN у Goldman Sachs",
        description=(
            "По просьбе Артёма необходимо получить обратную связь "
            "от Goldman Sachs по проекту PALADIN. Упомянуто, что "
            "сообщение могло попасть в спам, и важно выяснить, что "
            "происходит с их ответом. Это связано с обсуждением в "
            "дата руме, где они проявили интерес, но сейчас не "
            "отвечают"
        ),
        owner_user_id="111",
    )
    session.commit()

    backend = _FakeBackend(
        {
            "is_duplicate": True,
            "duplicate_of_task_id": existing,
            "reason": "same description, same Goldman Sachs feedback ask",
        }
    )
    out = check_duplicate(
        session,
        candidate={
            "title": "Взять обратную связь по PALADIN у Goldman Sachs",
            "description": (
                "По просьбе Артёма нужно получить обратную связь "
                "от Goldman Sachs по проекту PALADIN. Упомянуто, что "
                "сообщение могло попасть в спам, и важно выяснить, "
                "что происходит с их ответом. Это связано с "
                "обсуждением в дата руме, где они проявили интерес"
            ),
            "owner_user_id": "111",
        },
        llm_backend=backend,
    )
    assert out.is_duplicate is True
    # The LLM saw both descriptions IN FULL (no 200-char truncation).
    prompt = backend.last_user_prompt or ""
    assert "проявили интерес, но сейчас не отвечают" in prompt
    assert "не отвечают" in prompt or "проявили интерес" in prompt


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


def test_dedup_prompt_short_and_focused():
    """FR-CR-05-101 — operator wanted the prompt minimal.
    The «отчёт Ирине ≠ отчёт Артёму» rule from FR-CR-05-78 was
    stripped along with all other worked examples; the LLM is
    expected to handle it via the «compare descriptions» rule
    on its own. This test pins that the prompt stays minimal."""
    from app.services.task_dedup import _SYSTEM_PROMPT

    blob = _SYSTEM_PROMPT
    # Minimal: compare descriptions framing present.
    assert "descriptions" in blob.lower()
    # No synonym families anymore.
    for legacy in (
        "confirm-family",
        "meeting-family",
        "TRANSLITERATION",
        "ONE-EVENT COLLAPSE",
        "SYNONYM-VERBS",
    ):
        assert legacy not in blob, f"{legacy!r} should be gone in the minimal prompt"
