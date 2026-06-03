"""FR-TV (P2) — CEO-brain local tools for the Task Vector layer.

Same surface is reused by the external MCP server (FR-TV-090). Shape mirrors
`slack_tools.py`:
  - ``TASK_TOOL_SCHEMAS`` — Anthropic tool definitions (name/description/
    input_schema), 6 tools: search_tasks, get_task, resolve_person,
    update_task_status, update_task_due, update_task_owner.
  - ``build_task_executors(...)`` — returns ``{name: callable(input)->json_str}``.

SAFETY (see docs/SPEC_TASK_VECTOR_v0.1.md):
  - tools are id-precise — they cannot mass-update; the τ/δ confidence gate
    (FR-TV-043) is enforced by the agent before it calls an update tool.
  - update_* require the actor to be in CEO_BRAIN_ALLOWED_USERS (FR-TV-060).
  - every update returns {field, from, to, task_id} for generic UNDO (FR-TV-046).
  - executors NEVER raise — failures return {"error": ...}.
"""
from __future__ import annotations

import datetime as _dt
import json
from typing import Any, Callable

from app.logging_setup import get_logger
from app.models.entity_embedding import KIND_EMPLOYEE, KIND_TASK, KIND_TEAM_MEMBER

log = get_logger(__name__)

# FR-TV / DEC-5 — reviewable verb→status map (the agent uses it to infer the
# target status from an utterance; exposed as a constant so it's unit-testable).
VERB_STATUS_MAP: dict[str, str] = {
    # done
    "отправил": "done", "сделал": "done", "закрыл": "done",
    "завершил": "done", "выполнил": "done", "готово": "done",
    "отдал": "done", "сдал": "done",
    # in progress
    "начал": "in_progress", "приступил": "in_progress",
    "делаю": "in_progress", "занимаюсь": "in_progress",
    "вработе": "in_progress",
}

_VALID_STATUSES = {"backlog", "todo", "in_progress", "done"}

# FR-TV — read tools are always safe to expose; write tools mutate tasks and
# are gated behind `writes_enabled` (TASK_VECTOR_WRITES_ENABLED) so search/Q&A
# can go live read-only before the τ/δ confidence gate is calibrated.
READ_TOOL_NAMES = ("search_tasks", "get_task", "resolve_person")
WRITE_TOOL_NAMES = ("update_task_status", "update_task_due", "update_task_owner")


def task_tool_schemas(*, writes_enabled: bool) -> list[dict[str, Any]]:
    """Subset of TASK_TOOL_SCHEMAS the agent may see. Read-only by default;
    write-tool schemas are included only when `writes_enabled` is True."""
    allow = set(READ_TOOL_NAMES) | (set(WRITE_TOOL_NAMES) if writes_enabled else set())
    return [s for s in TASK_TOOL_SCHEMAS if s["name"] in allow]


TASK_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "search_tasks",
        "description": (
            "Семантический поиск задач по смыслу (вектор по каталогу задач). "
            "Возвращает топ-K задач с ЖИВЫМИ полями (status, owner, due_date) и "
            "score. Используй для вопросов «какие задачи по X / что на Y / что "
            "просрочено» и чтобы найти задачу перед обновлением."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Тема/описание/имя."},
                "k": {"type": "integer", "default": 10},
                "status": {"type": "string", "description": "Фильтр по статусу."},
                "owner": {"type": "string", "description": "Фильтр по ответственному."},
                "overdue": {"type": "boolean", "description": "Только просроченные."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_task",
        "description": "Полные поля одной задачи по её id.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "resolve_person",
        "description": (
            "Найти человека из команды по имени (вектор по team_member+employee). "
            "Вернёт кандидатов с person_id/display_name/role/score. Нужен перед "
            "сменой ответственного."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "k": {"type": "integer", "default": 10},
            },
            "required": ["name"],
        },
    },
    {
        "name": "update_task_status",
        "description": (
            "Сменить статус задачи (backlog/todo/in_progress/done). Вызывай ТОЛЬКО "
            "для одной уверенно найденной задачи. Возвращает {field,from,to} для "
            "отмены."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "new_status": {"type": "string", "enum": sorted(_VALID_STATUSES)},
                "reason": {"type": "string"},
                "actor_id": {"type": "string", "description": "Slack user id инициатора."},
            },
            "required": ["task_id", "new_status", "actor_id"],
        },
    },
    {
        "name": "update_task_due",
        "description": (
            "Перенести срок задачи. due_date — ISO YYYY-MM-DD (дату из «пятница/"
            "завтра» разбери САМ в ISO). Возвращает {field,from,to}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "due_date": {"type": "string", "description": "ISO YYYY-MM-DD."},
                "due_time": {"type": "string", "description": "ISO HH:MM (опц.)."},
                "reason": {"type": "string"},
                "actor_id": {"type": "string"},
            },
            "required": ["task_id", "due_date", "actor_id"],
        },
    },
    {
        "name": "update_task_owner",
        "description": (
            "Сменить ответственного. Передавай owner_user_id/owner_display_name, "
            "полученные из resolve_person. Возвращает {field,from,to}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "owner_user_id": {"type": "string"},
                "owner_display_name": {"type": "string"},
                "reason": {"type": "string"},
                "actor_id": {"type": "string"},
            },
            "required": ["task_id", "owner_user_id", "owner_display_name", "actor_id"],
        },
    },
]


def _allowed_users(settings: Any) -> set[str]:
    raw = getattr(settings, "ceo_brain_allowed_users", "") or ""
    return {u.strip() for u in raw.split(",") if u.strip()}


def build_task_executors(
    *,
    session_factory: Callable[[], Any],
    settings: Any,
    search_fn: Callable[[str, str, int], list[dict[str, Any]]] | None = None,
    sync_fn: Callable[[int], None] | None = None,
    writes_enabled: bool = False,
) -> dict[str, Callable[[dict[str, Any]], str]]:
    """name→callable map. Dependencies are injectable for tests:
    `search_fn(query, kind, k)`→candidates (default: pgvector over the task
    vector DB), `sync_fn(task_id)` (default: app.sync.task_sync.sync_task).

    `writes_enabled=False` (default) returns ONLY the read tools
    (search_tasks/get_task/resolve_person); the three update_* tools are
    omitted entirely so they can never be invoked until writes are turned on."""

    allowed = _allowed_users(settings)
    default_k = int(getattr(settings, "task_vector_k", 10) or 10)

    def _ok(payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False, default=str)

    def _err(msg: str, **extra: Any) -> str:
        return _ok({"error": msg, **extra})

    # ---- search backend (lazy default over the task vector DB) -------------
    def _search(query: str, kind: str, k: int) -> list[dict[str, Any]]:
        if search_fn is not None:
            return search_fn(query, kind, k)
        from openai import OpenAI

        from app.db import get_task_vector_session_factory
        from app.services.entity_embeddings import (
            make_openai_embed_fn,
            search_entities,
        )

        vf = get_task_vector_session_factory()
        sess = vf() if vf is not None else session_factory()
        owns = vf is not None
        try:
            model = getattr(settings, "task_vector_model", "text-embedding-3-large")
            embed = make_openai_embed_fn(
                OpenAI(api_key=settings.openai_api_key), model=model
            )
            return search_entities(
                sess, kind=kind, query_text=query, embed_fn=embed,
                model=model, k=k,
            )
        finally:
            if owns:
                sess.rollback()
                sess.close()

    def _sync(task_id: int) -> None:
        if sync_fn is not None:
            sync_fn(task_id)
            return
        try:
            from app.sync.task_sync import sync_task
            sync_task(task_id)
        except Exception as e:  # noqa: BLE001 — sync is best-effort
            log.warning("task_tool_sync_failed", task_id=task_id, error=str(e))

    # ---- read tools --------------------------------------------------------
    def search_tasks(inp: dict) -> str:
        try:
            from app.models.task import Task

            query = (inp.get("query") or "").strip()
            if not query:
                return _ok([])
            k = int(inp.get("k") or default_k)
            hits = _search(query, KIND_TASK, k)
            ids = [int(h["entity_id"]) for h in hits
                   if str(h.get("entity_id", "")).isdigit()]
            score_by = {int(h["entity_id"]): float(h.get("score", 0.0))
                        for h in hits if str(h.get("entity_id", "")).isdigit()}
            out: list[dict[str, Any]] = []
            sess = session_factory()
            try:
                rows = (sess.query(Task)
                        .filter(Task.id.in_(ids), Task.deleted_at.is_(None)).all()
                        if ids else [])
                today = _dt.date.today()
                f_status = inp.get("status")
                f_owner = (inp.get("owner") or "").strip().lower() or None
                f_overdue = bool(inp.get("overdue"))
                for t in rows:
                    st = getattr(t.status, "value", t.status)
                    if f_status and st != f_status:
                        continue
                    if f_owner and f_owner not in (
                        (t.owner_display_name or "").lower()
                        + " " + (t.owner_user_id or "").lower()
                    ):
                        continue
                    if f_overdue and not (t.due_date and t.due_date < today
                                          and st != "done"):
                        continue
                    out.append({
                        "task_id": t.id, "title": t.title, "status": st,
                        "owner_display_name": t.owner_display_name,
                        "due_date": t.due_date, "category": t.category,
                        "score": round(score_by.get(t.id, 0.0), 4),
                    })
            finally:
                sess.rollback()
                sess.close()
            out.sort(key=lambda r: r["score"], reverse=True)
            log.info("task_search", query=query[:80], k=k, hits=len(out))
            return _ok(out)
        except Exception as e:  # noqa: BLE001 — NFR-TV-005 never raises
            log.error("task_search_failed", error=str(e))
            return _err(f"search_failed: {e}")

    def get_task(inp: dict) -> str:
        try:
            from app.models.task import Task

            tid = int(inp["task_id"])
            sess = session_factory()
            try:
                t = sess.get(Task, tid)
                if t is None or t.deleted_at is not None:
                    return _err("not_found", task_id=tid)
                return _ok({
                    "task_id": t.id, "title": t.title,
                    "description": t.description,
                    "status": getattr(t.status, "value", t.status),
                    "owner_display_name": t.owner_display_name,
                    "owner_user_id": t.owner_user_id,
                    "due_date": t.due_date, "due_time": t.due_time,
                    "category": t.category, "priority": getattr(
                        t.priority, "value", t.priority),
                })
            finally:
                sess.rollback()
                sess.close()
        except Exception as e:  # noqa: BLE001
            log.error("get_task_failed", error=str(e))
            return _err(f"get_task_failed: {e}")

    def resolve_person(inp: dict) -> str:
        try:
            name = (inp.get("name") or "").strip()
            if not name:
                return _ok([])
            k = int(inp.get("k") or default_k)
            cands: list[dict[str, Any]] = []
            for kind in (KIND_TEAM_MEMBER, KIND_EMPLOYEE):
                for h in _search(name, kind, k):
                    cands.append({
                        "person_id": h.get("entity_id"),
                        "kind": kind,
                        "text": h.get("text_repr"),
                        "score": round(float(h.get("score", 0.0)), 4),
                    })
            cands.sort(key=lambda r: r["score"], reverse=True)
            log.info("resolve_person", name=name[:60], hits=len(cands))
            return _ok(cands[:k])
        except Exception as e:  # noqa: BLE001
            log.error("resolve_person_failed", error=str(e))
            return _err(f"resolve_person_failed: {e}")

    # ---- write tools (allow-listed, audited, id-precise) -------------------
    def _guard(actor_id: str | None) -> str | None:
        if not actor_id or actor_id not in allowed:
            log.warning("task_write_forbidden", actor_id=actor_id)
            return _err("forbidden")
        return None

    def update_task_status(inp: dict) -> str:
        actor = inp.get("actor_id")
        denied = _guard(actor)
        if denied:
            return denied
        try:
            from app.models.task import Task, TaskStatus
            from app.services.transitions import InvalidTransition, TransitionService

            new = (inp.get("new_status") or "").strip()
            if new not in _VALID_STATUSES:
                return _err("invalid_status", to=new)
            tid = int(inp["task_id"])
            sess = session_factory()
            try:
                t = sess.get(Task, tid)
                if t is None or t.deleted_at is not None:
                    return _err("not_found", task_id=tid)
                title = t.title
                old = getattr(t.status, "value", t.status)
                if old == new:  # FR-TV-044 idempotent no-op
                    return _ok({"ok": True, "changed": False, "field": "status",
                                "from": old, "to": new, "task_id": tid,
                                "title": title})
                try:
                    TransitionService().apply(
                        sess, task=t, new_status=TaskStatus(new),
                        actor_slack_user_id=actor, reason=inp.get("reason"),
                    )
                except InvalidTransition as e:
                    sess.rollback()
                    return _err("invalid_transition", **{"from": old, "to": new,
                                                          "detail": str(e)})
                sess.commit()
            finally:
                sess.close()
            _sync(tid)
            # FR-ST-LOG — unified status-event log (never breaks the update)
            from app.services.status_events import record_status_event_safe
            record_status_event_safe(
                session_factory, task_id=tid, source="chat", actor=actor,
                field="status", from_value=str(old), to_value=str(new),
            )
            log.info("task_status_update", task_id=tid, **{"from": old}, to=new,
                     actor=actor, ok=True)
            return _ok({"ok": True, "changed": True, "field": "status",
                        "from": old, "to": new, "task_id": tid, "title": title})
        except Exception as e:  # noqa: BLE001
            log.error("task_status_update_failed", error=str(e))
            return _err(f"update_failed: {e}")

    def update_task_due(inp: dict) -> str:
        actor = inp.get("actor_id")
        denied = _guard(actor)
        if denied:
            return denied
        try:
            from app.models.task import Task

            try:
                new_due = _dt.date.fromisoformat((inp.get("due_date") or "").strip())
            except ValueError:
                return _err("invalid_date", due_date=inp.get("due_date"))
            new_time = None
            if inp.get("due_time"):
                try:
                    new_time = _dt.time.fromisoformat(inp["due_time"].strip())
                except ValueError:
                    return _err("invalid_time", due_time=inp.get("due_time"))
            tid = int(inp["task_id"])
            sess = session_factory()
            try:
                t = sess.get(Task, tid)
                if t is None or t.deleted_at is not None:
                    return _err("not_found", task_id=tid)
                old = t.due_date.isoformat() if t.due_date else None
                t.due_date = new_due
                if new_time is not None:
                    t.due_time = new_time
                sess.commit()
            finally:
                sess.close()
            _sync(tid)
            # FR-ST-LOG — unified status-event log (never breaks the update)
            from app.services.status_events import record_status_event_safe
            record_status_event_safe(
                session_factory, task_id=tid, source="chat", actor=actor,
                field="due_date", from_value=old, to_value=new_due.isoformat(),
            )
            log.info("task_due_update", task_id=tid, **{"from": old},
                     to=new_due.isoformat(), actor=actor)
            return _ok({"ok": True, "changed": True, "field": "due_date",
                        "from": old, "to": new_due.isoformat(), "task_id": tid})
        except Exception as e:  # noqa: BLE001
            log.error("task_due_update_failed", error=str(e))
            return _err(f"update_failed: {e}")

    def update_task_owner(inp: dict) -> str:
        actor = inp.get("actor_id")
        denied = _guard(actor)
        if denied:
            return denied
        try:
            from app.models.task import Task

            owner_id = (inp.get("owner_user_id") or "").strip()
            owner_name = (inp.get("owner_display_name") or "").strip()
            if not owner_id or not owner_name:
                return _err("missing_owner")
            tid = int(inp["task_id"])
            sess = session_factory()
            try:
                t = sess.get(Task, tid)
                if t is None or t.deleted_at is not None:
                    return _err("not_found", task_id=tid)
                old = {"owner_user_id": t.owner_user_id,
                       "owner_display_name": t.owner_display_name}
                t.owner_user_id = owner_id
                t.owner_display_name = owner_name
                sess.commit()
            finally:
                sess.close()
            _sync(tid)
            # FR-ST-LOG — unified status-event log (never breaks the update)
            from app.services.status_events import record_status_event_safe
            record_status_event_safe(
                session_factory, task_id=tid, source="chat", actor=actor,
                field="owner", from_value=old,
                to_value={"owner_user_id": owner_id, "owner_display_name": owner_name},
            )
            log.info("task_owner_update", task_id=tid, **{"from": old["owner_display_name"]},
                     to=owner_name, actor=actor)
            return _ok({"ok": True, "changed": True, "field": "owner",
                        "from": old, "to": {"owner_user_id": owner_id,
                                            "owner_display_name": owner_name},
                        "task_id": tid})
        except Exception as e:  # noqa: BLE001
            log.error("task_owner_update_failed", error=str(e))
            return _err(f"update_failed: {e}")

    execs: dict[str, Callable[[dict[str, Any]], str]] = {
        "search_tasks": search_tasks,
        "get_task": get_task,
        "resolve_person": resolve_person,
    }
    if writes_enabled:
        execs["update_task_status"] = update_task_status
        execs["update_task_due"] = update_task_due
        execs["update_task_owner"] = update_task_owner
    return execs


__all__ = [
    "READ_TOOL_NAMES",
    "TASK_TOOL_SCHEMAS",
    "VERB_STATUS_MAP",
    "WRITE_TOOL_NAMES",
    "build_task_executors",
    "task_tool_schemas",
]
