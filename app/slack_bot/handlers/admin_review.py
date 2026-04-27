"""Admin review action handlers (CR-03 FR-CR-03-4, FR-CR-03-5).

Three buttons on the admin review card:
- Confirm → no state change, just ack + audit; the card is replaced with
  a "подтверждено" note.
- Edit    → opens a modal prefilled with the task's current values.
- Reject  → deletes the task from DB, replaces the card with "отклонено",
  writes an audit row. Subscribers are notified by the regular DM.
"""
from __future__ import annotations

from typing import Any

from slack_bolt import Ack
from slack_sdk import WebClient

from app.db import session_scope
from app.logging_setup import get_logger
from app.models import AuditLog, Task
from app.services import is_admin
from app.services.card_sync import refresh_task_card
from app.slack_bot import blocks as bk

log = get_logger(__name__)


def _task_id(body: dict[str, Any]) -> int | None:
    try:
        return int((body.get("actions") or [{}])[0].get("value") or 0) or None
    except (TypeError, ValueError):
        return None


def _actor(body: dict[str, Any]) -> str | None:
    return (body.get("user") or {}).get("id")


def _channel(body: dict[str, Any]) -> str | None:
    return (body.get("channel") or {}).get("id")


def _message_ts(body: dict[str, Any]) -> str | None:
    return (body.get("message") or {}).get("ts")


def _replace_card(
    *, sender, body: dict[str, Any], task_id: int, action: str, actor: str | None
) -> None:
    channel = _channel(body)
    ts = _message_ts(body)
    if not channel or not ts:
        return
    try:
        sender.update_message(
            channel=channel,
            ts=ts,
            blocks=bk.admin_review_resolved_message(
                task_id=task_id, action=action, actor=actor
            ),
            text=f":clipboard: Task #{task_id} — {action}",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("admin_review_card_update_failed", error=str(e))


def _gate_admin(body: dict[str, Any], sender) -> str | None:
    """Reject clicks from non-admins. Returns the actor id if allowed."""
    actor = _actor(body)
    if not is_admin(actor):
        channel = _channel(body)
        if channel and actor:
            try:
                sender.post_ephemeral(
                    channel=channel,
                    user=actor,
                    text=":lock: Эту кнопку видит только админ.",
                )
            except Exception:  # noqa: BLE001
                pass
        return None
    return actor


def handle_admin_confirm(
    *, body: dict[str, Any], sender, ack: Ack
) -> None:
    ack()
    task_id = _task_id(body)
    if task_id is None:
        return
    actor = _gate_admin(body, sender)
    if actor is None:
        return
    with session_scope() as session:
        session.add(
            AuditLog(
                category="admin_review",
                action="admin_confirmed",
                entity_type="task",
                entity_id=str(task_id),
                actor=actor,
            )
        )
    _replace_card(
        sender=sender, body=body, task_id=task_id, action="confirm", actor=actor
    )


def handle_admin_reject(
    *, body: dict[str, Any], sender, ack: Ack
) -> None:
    ack()
    task_id = _task_id(body)
    if task_id is None:
        return
    actor = _gate_admin(body, sender)
    if actor is None:
        return

    with session_scope() as session:
        task = session.get(Task, task_id)
        if task is None:
            _replace_card(
                sender=sender, body=body, task_id=task_id, action="reject", actor=actor
            )
            return
        title = task.title
        session.delete(task)
        session.add(
            AuditLog(
                category="admin_review",
                action="admin_rejected",
                entity_type="task",
                entity_id=str(task_id),
                actor=actor,
                payload={"title": title},
            )
        )
    _replace_card(
        sender=sender, body=body, task_id=task_id, action="reject", actor=actor
    )


def handle_admin_edit_open(
    *, body: dict[str, Any], client: WebClient, sender, ack: Ack
) -> None:
    ack()
    task_id = _task_id(body)
    trigger_id = body.get("trigger_id")
    if task_id is None or not trigger_id:
        return
    actor = _gate_admin(body, sender)
    if actor is None:
        return
    with session_scope() as session:
        task = session.get(Task, task_id)
        if task is None:
            return
        initial = {
            "title": task.title,
            "description": task.description,
            "owner_user_id": task.owner_user_id,
            "owner_display_name": task.owner_display_name,
            "priority": task.priority.value,
            "due_date": task.due_date.isoformat() if task.due_date else None,
            "due_time": task.due_time.strftime("%H:%M") if task.due_time else None,
            "estimated_minutes": task.estimated_minutes,
        }

    import json as _json

    from app.config import get_settings

    view = bk.task_modal(
        private_metadata=_json.dumps(
            {"edit_task_id": task_id, "admin_review_msg": {
                "channel": _channel(body), "ts": _message_ts(body),
            }}
        ),
        initial=initial,
        allowed_owners=get_settings().allowed_owners(),
    )
    # Override the callback id so the submit handler knows this is an
    # admin-edit flow.
    view["callback_id"] = bk.MODAL_CALLBACK_ADMIN_EDIT
    try:
        client.views_open(trigger_id=trigger_id, view=view)
    except Exception as e:  # noqa: BLE001
        log.warning("admin_edit_views_open_failed", error=str(e))


def handle_admin_edit_submit(
    *, body: dict[str, Any], view: dict[str, Any], sender, ack: Ack
) -> None:
    from app.slack_bot.handlers.views import _extract_task_payload

    payload = _extract_task_payload(view)
    if not payload["title"]:
        ack(
            response_action="errors",
            errors={bk.BLOCK_TITLE: "Title is required"},
        )
        return
    ack()

    import json as _json

    pm = _json.loads(view.get("private_metadata") or "{}")
    task_id = pm.get("edit_task_id")
    admin_msg = pm.get("admin_review_msg") or {}
    actor = _actor(body)
    if not task_id:
        return

    from datetime import date as _date

    with session_scope() as session:
        task = session.get(Task, int(task_id))
        if task is None:
            return
        diff: dict[str, Any] = {}
        if payload["title"] != task.title:
            diff["title"] = [task.title, payload["title"]]
            task.title = payload["title"]
        if payload.get("description") != task.description:
            diff["description"] = [task.description, payload.get("description")]
            task.description = payload.get("description")
        if payload.get("owner_user_id") and payload["owner_user_id"] != task.owner_user_id:
            diff["owner_user_id"] = [task.owner_user_id, payload["owner_user_id"]]
            task.owner_user_id = payload["owner_user_id"]
        if payload.get("owner_display_name") and payload["owner_display_name"] != task.owner_display_name:
            diff["owner_display_name"] = [
                task.owner_display_name,
                payload["owner_display_name"],
            ]
            task.owner_display_name = payload["owner_display_name"]
        new_prio = payload.get("priority")
        if new_prio and new_prio != task.priority.value:
            diff["priority"] = [task.priority.value, new_prio]
            from app.models.task import TaskPriority

            task.priority = TaskPriority(new_prio)
        new_due = payload.get("due_date")
        new_due_d = None
        if isinstance(new_due, str) and new_due:
            try:
                new_due_d = _date.fromisoformat(new_due)
            except ValueError:
                new_due_d = None
        if new_due_d != task.due_date:
            diff["due_date"] = [
                task.due_date.isoformat() if task.due_date else None,
                new_due_d.isoformat() if new_due_d else None,
            ]
            task.due_date = new_due_d

        from datetime import time as _time

        new_time = payload.get("due_time")
        new_time_t = None
        if isinstance(new_time, str) and new_time:
            try:
                hh, mm = new_time.split(":")[:2]
                new_time_t = _time(int(hh), int(mm))
            except (ValueError, IndexError):
                new_time_t = None
        if new_time_t != task.due_time:
            diff["due_time"] = [
                task.due_time.strftime("%H:%M") if task.due_time else None,
                new_time_t.strftime("%H:%M") if new_time_t else None,
            ]
            task.due_time = new_time_t

        if payload.get("estimated_minutes") != task.estimated_minutes:
            diff["estimated_minutes"] = [
                task.estimated_minutes,
                payload.get("estimated_minutes"),
            ]
            task.estimated_minutes = payload.get("estimated_minutes")

        if diff:
            session.add(
                AuditLog(
                    category="admin_review",
                    action="task_edited",
                    entity_type="task",
                    entity_id=str(task.id),
                    actor=actor,
                    payload={"diff": diff},
                )
            )
        session.flush()
        # Refresh the user-facing task card (channel + DM) so edits are live.
        if hasattr(sender, "update_message"):
            refresh_task_card(sender, task)

    # Replace the admin-review DM/ephemeral to show it's been handled.
    if admin_msg.get("ts") and admin_msg.get("channel"):
        try:
            sender.update_message(
                channel=admin_msg["channel"],
                ts=admin_msg["ts"],
                blocks=bk.admin_review_resolved_message(
                    task_id=int(task_id), action="edit", actor=actor
                ),
                text="edited",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("admin_edit_followup_update_failed", error=str(e))
