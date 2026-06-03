"""FR-ST-LOG / FR-ST-RB — write to the unified task-status event log and
roll back from it (SPEC_STATUS_TRACKER_v0.2 §2/§3).

Three entry points:
  * ``record_status_event(session, ...)`` — append one event (caller commits).
  * ``record_status_event_safe(session_factory, ...)`` — own session, commit,
    NEVER raises (used as a fire-and-forget hook from the Task Vector write
    tools — logging must never break the actual update).
  * ``rollback_event(session, event_id=...)`` — restore an event's
    ``from_value`` via the normal write path and log a new ``source=rollback``
    event. Append-only is preserved.
"""
from __future__ import annotations

import datetime as _dt
import json
from typing import Any, Callable

from app.logging_setup import get_logger
from app.models.task_status_event import TaskStatusEvent

log = get_logger(__name__)

_VALID_FIELDS = {"status", "due_date", "owner", "comment"}


def _as_text(v: Any) -> str | None:
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, (dict, list)):
        try:
            return json.dumps(v, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            return str(v)
    return str(v)


def record_status_event(
    session: Any,
    *,
    task_id: int,
    source: str,
    field: str,
    from_value: Any = None,
    to_value: Any = None,
    actor: str | None = None,
    comment: str | None = None,
    raw_quote: str | None = None,
    confidence: float | None = None,
    meeting_ref: dict | None = None,
    applied: bool = True,
) -> TaskStatusEvent:
    """Append one event. Caller is responsible for commit. Owner dict values
    are JSON-encoded into from/to_value so they round-trip for rollback."""
    mref = meeting_ref or None
    ev = TaskStatusEvent(
        task_id=task_id,
        source=source,
        field=field,
        from_value=_as_text(from_value),
        to_value=_as_text(to_value),
        actor=actor,
        comment=comment,
        raw_quote=raw_quote,
        confidence=confidence,
        meeting_source_id=(mref or {}).get("source_id"),
        meeting_segment_idx=(mref or {}).get("segment_idx"),
        meeting_ref=mref,
        applied=applied,
    )
    session.add(ev)
    session.flush()
    return ev


def record_status_event_safe(session_factory: Callable[[], Any], **kw: Any) -> None:
    """Fire-and-forget. Opens its own session, commits, and swallows every
    error (including the meeting-idempotency UNIQUE clash → no-op). The caller
    can invoke this right after a successful update without any guard."""
    try:
        from sqlalchemy.exc import IntegrityError

        sess = session_factory()
        try:
            record_status_event(sess, **kw)
            sess.commit()
        except IntegrityError:
            sess.rollback()  # duplicate meeting event — idempotent no-op
        except Exception as e:  # noqa: BLE001
            sess.rollback()
            log.warning("status_event_log_failed", error=str(e))
        finally:
            sess.close()
    except Exception as e:  # noqa: BLE001 — never propagate
        log.warning("status_event_log_session_failed", error=str(e))


def rollback_event(
    session: Any,
    *,
    event_id: int,
    actor: str = "system",
    sync: Callable[[int], None] | None = None,
) -> dict:
    """Restore the pre-change value of ``event_id`` and log a new
    ``source=rollback`` event. ``sync`` (optional) propagates to
    Sheets/Tasks/cards — passed by the ops CLI, omitted in unit tests."""
    from app.models.task import Task

    ev = session.get(TaskStatusEvent, event_id)
    if ev is None:
        return {"ok": False, "error": "event_not_found", "event_id": event_id}
    t = session.get(Task, ev.task_id)
    if t is None or t.deleted_at is not None:
        return {"ok": False, "error": "task_not_found", "task_id": ev.task_id}

    field = ev.field
    restore = ev.from_value  # the value as it was BEFORE the logged change
    if field not in _VALID_FIELDS or field == "comment":
        return {"ok": False, "error": f"field_not_rollbackable: {field}"}

    if field == "status":
        from app.models.task import TaskStatus
        from app.services.transitions import InvalidTransition, TransitionService

        was = getattr(t.status, "value", t.status)
        if not restore:
            return {"ok": False, "error": "no_prior_status"}
        try:
            TransitionService().apply(
                session,
                task=t,
                new_status=TaskStatus(restore),
                actor_slack_user_id=actor,
                reason=f"rollback of event {event_id}",
            )
        except InvalidTransition as e:
            session.rollback()
            return {"ok": False, "error": f"invalid_transition: {e}"}
    elif field == "due_date":
        was = t.due_date.isoformat() if t.due_date else None
        t.due_date = _dt.date.fromisoformat(restore) if restore else None
    elif field == "owner":
        was = json.dumps(
            {"owner_user_id": t.owner_user_id, "owner_display_name": t.owner_display_name},
            ensure_ascii=False,
        )
        d = json.loads(restore) if restore else {}
        t.owner_user_id = d.get("owner_user_id")
        t.owner_display_name = d.get("owner_display_name")

    record_status_event(
        session,
        task_id=t.id,
        source="rollback",
        field=field,
        from_value=was,
        to_value=restore,
        actor=actor,
        comment=f"rollback of event {event_id}",
    )
    session.commit()

    if sync is not None:
        try:
            sync(t.id)
        except Exception as e:  # noqa: BLE001 — sync failure must not undo the rollback
            log.warning("rollback_sync_failed", task_id=t.id, error=str(e))

    return {
        "ok": True,
        "task_id": t.id,
        "field": field,
        "restored_to": restore,
        "was": was,
        "rolled_back_event": event_id,
    }


__all__ = ["record_status_event", "record_status_event_safe", "rollback_event"]
