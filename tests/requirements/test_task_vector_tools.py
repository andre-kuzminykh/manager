"""FR-TV — Task Vector CEO-brain tools (see docs/SPEC_TASK_VECTOR_v0.1.md).

Real unit tests on the in-memory SQLite session (conftest `sm`),
with FAKE search + FAKE sync injected — no network, no pgvector, no LLM.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

tt = pytest.importorskip("app.ceo_brain.task_tools")


@pytest.fixture()
def sm():
    """sessionmaker over a SHARED in-memory SQLite (StaticPool) so the tool's
    own sessions and the test's assertions hit the same DB."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.models import Base

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class _Settings:
    ceo_brain_allowed_users = "U_BOSS"
    task_vector_k = 10
    task_vector_model = "text-embedding-3-large"
    openai_api_key = ""


def _mk_task(sm, *, title="Отправить договор Семёну", owner="Семён", status=None):
    from app.models.task import Task, TaskStatus
    s = sm()
    try:
        t = Task(title=title, owner_display_name=owner,
                 status=status or TaskStatus.todo)
        s.add(t)
        s.commit()
        return t.id
    finally:
        s.close()


def _execs(sm, *, calls=None, search=None):
    return tt.build_task_executors(
        session_factory=sm, settings=_Settings(), search_fn=search,
        sync_fn=(calls.append if calls is not None else (lambda i: None)),
    )


# ---- schemas / constants (no DB) -----------------------------------------
def test_schemas_and_executors_present():
    names = {s["name"] for s in tt.TASK_TOOL_SCHEMAS}
    assert {"search_tasks", "get_task", "resolve_person", "update_task_status",
            "update_task_due", "update_task_owner"} <= names
    for s in tt.TASK_TOOL_SCHEMAS:
        assert s.get("description") and s.get("input_schema")


def test_verb_status_map_constant():
    assert set(tt.VERB_STATUS_MAP.values()) <= {"backlog", "todo", "in_progress", "done"}
    assert tt.VERB_STATUS_MAP.get("отправил") == "done"
    assert tt.VERB_STATUS_MAP.get("начал") == "in_progress"


# ---- search (live fields, ordered) ---------------------------------------
def test_search_tasks_returns_live_fields(sm):
    tid = _mk_task(sm)
    search = lambda q, kind, k: [{"entity_id": str(tid), "score": 0.81}]
    ex = _execs(sm, search=search)
    out = json.loads(ex["search_tasks"]({"query": "договор"}))
    assert len(out) == 1
    assert out[0]["task_id"] == tid and out[0]["status"] == "todo"
    assert out[0]["owner_display_name"] == "Семён" and out[0]["score"] == 0.81


def test_search_overdue_filter(sm):
    from app.models.task import Task, TaskStatus
    s = sm()
    t = Task(title="старая", status=TaskStatus.todo,
             due_date=dt.date(2000, 1, 1))
    s.add(t); s.commit(); tid = t.id; s.close()
    ex = _execs(sm, search=lambda q, kk, k: [{"entity_id": str(tid), "score": 0.5}])
    assert len(json.loads(ex["search_tasks"]({"query": "x", "overdue": True}))) == 1
    # a done task is never overdue
    s = sm(); t2 = s.get(Task, tid); t2.status = TaskStatus.done; s.commit(); s.close()
    assert json.loads(ex["search_tasks"]({"query": "x", "overdue": True})) == []


# ---- status update -------------------------------------------------------
def test_update_status_forbidden_for_unlisted_actor(sm):
    tid = _mk_task(sm)
    ex = _execs(sm)
    r = json.loads(ex["update_task_status"]({"task_id": tid, "new_status": "done",
                                             "actor_id": "U_RANDOM"}))
    assert r["error"] == "forbidden"


def test_update_status_unknown_id(sm):
    ex = _execs(sm)
    r = json.loads(ex["update_task_status"]({"task_id": 999999, "new_status": "done",
                                             "actor_id": "U_BOSS"}))
    assert r["error"] == "not_found"


def test_update_status_invalid_status(sm):
    tid = _mk_task(sm)
    ex = _execs(sm)
    r = json.loads(ex["update_task_status"]({"task_id": tid, "new_status": "shipped",
                                             "actor_id": "U_BOSS"}))
    assert r["error"] == "invalid_status"


def test_update_status_idempotent_noop(sm):
    from app.models.task import TaskStatus
    tid = _mk_task(sm, status=TaskStatus.todo)
    calls: list[int] = []
    ex = _execs(sm, calls=calls)
    r = json.loads(ex["update_task_status"]({"task_id": tid, "new_status": "todo",
                                             "actor_id": "U_BOSS"}))
    assert r["ok"] is True and r["changed"] is False


def test_update_status_happy_changes_and_syncs(sm):
    from app.models.task import Task, TaskStatus, TaskStatusHistory
    tid = _mk_task(sm, status=TaskStatus.todo)
    calls: list[int] = []
    ex = _execs(sm, calls=calls)
    r = json.loads(ex["update_task_status"]({"task_id": tid, "new_status": "done",
                                             "actor_id": "U_BOSS", "reason": "почта"}))
    assert r["ok"] and r["changed"] and r["field"] == "status"
    assert r["from"] == "todo" and r["to"] == "done"
    assert calls == [tid]                                   # sync fired once
    s = sm()
    try:
        assert s.get(Task, tid).status == TaskStatus.done
        hist = s.query(TaskStatusHistory).filter_by(task_id=tid).all()
        assert any(h.to_status == TaskStatus.done for h in hist)
    finally:
        s.close()


# ---- due update ----------------------------------------------------------
def test_update_due_invalid_date(sm):
    tid = _mk_task(sm)
    ex = _execs(sm)
    r = json.loads(ex["update_task_due"]({"task_id": tid, "due_date": "пятница",
                                          "actor_id": "U_BOSS"}))
    assert r["error"] == "invalid_date"


def test_update_due_valid(sm):
    from app.models.task import Task
    tid = _mk_task(sm)
    calls: list[int] = []
    ex = _execs(sm, calls=calls)
    r = json.loads(ex["update_task_due"]({"task_id": tid, "due_date": "2026-06-12",
                                          "actor_id": "U_BOSS"}))
    assert r["ok"] and r["field"] == "due_date" and r["to"] == "2026-06-12"
    assert calls == [tid]
    s = sm()
    try:
        assert s.get(Task, tid).due_date == dt.date(2026, 6, 12)
    finally:
        s.close()


# ---- owner update --------------------------------------------------------
def test_update_owner_sets_and_reports(sm):
    from app.models.task import Task
    tid = _mk_task(sm, owner="старый")
    calls: list[int] = []
    ex = _execs(sm, calls=calls)
    r = json.loads(ex["update_task_owner"]({"task_id": tid, "owner_user_id": "U_SEMEN",
                                            "owner_display_name": "Семён Седов",
                                            "actor_id": "U_BOSS"}))
    assert r["ok"] and r["field"] == "owner"
    assert r["to"]["owner_display_name"] == "Семён Седов"
    assert calls == [tid]
    s = sm()
    try:
        t = s.get(Task, tid)
        assert t.owner_user_id == "U_SEMEN" and t.owner_display_name == "Семён Седов"
    finally:
        s.close()


def test_update_owner_forbidden(sm):
    tid = _mk_task(sm)
    ex = _execs(sm)
    r = json.loads(ex["update_task_owner"]({"task_id": tid, "owner_user_id": "U1",
                                            "owner_display_name": "X",
                                            "actor_id": "nope"}))
    assert r["error"] == "forbidden"


# ---- resolve_person (fake vector over team kinds) ------------------------
def test_resolve_person_ranks_team(sm):
    def search(name, kind, k):
        if kind == "team_member":
            return [{"entity_id": "7", "text_repr": "Семён Седов. role: sales", "score": 0.7}]
        return [{"entity_id": "U_E", "text_repr": "Semyon", "score": 0.4}]
    ex = _execs(sm, search=search)
    out = json.loads(ex["resolve_person"]({"name": "Семён"}))
    assert out[0]["person_id"] == "7" and out[0]["score"] == 0.7   # team_member ranks first
    assert {c["kind"] for c in out} == {"team_member", "employee"}


# ---- never-raises (NFR-TV-005) -------------------------------------------
def test_search_never_raises_on_backend_failure(sm):
    def boom(q, kind, k):
        raise RuntimeError("vector down")
    ex = _execs(sm, search=boom)
    r = json.loads(ex["search_tasks"]({"query": "x"}))
    assert "error" in r          # swallowed, structured error
