"""Action handlers for the evening daily-plan DM (Skip / Approve)."""
from __future__ import annotations

from datetime import date
from typing import Any

from slack_bolt import Ack

from app.db import session_scope
from app.logging_setup import get_logger
from app.models import AuditLog, DailyPlanItem
from app.services.daily_plan import skip_plan_item

log = get_logger(__name__)


def _actor(body: dict[str, Any]) -> str | None:
    return (body.get("user") or {}).get("id")


def _channel(body: dict[str, Any]) -> str | None:
    return (body.get("channel") or {}).get("id")


def _message_ts(body: dict[str, Any]) -> str | None:
    return (body.get("message") or {}).get("ts")


def handle_plan_skip(*, body: dict[str, Any], sender, ack: Ack) -> None:
    """Skip click on the evening plan card. Looks up the
    DailyPlanItem by (user, plan_date, task_id) — the value carries
    plan_date|task_id — and sets excluded_at."""
    ack()
    actor = _actor(body)
    if not actor:
        return
    raw = (body.get("actions") or [{}])[0].get("value") or ""
    try:
        plan_date_str, task_id_str = raw.split("|", 1)
        plan_date = date.fromisoformat(plan_date_str)
        task_id = int(task_id_str)
    except (ValueError, IndexError):
        return

    with session_scope() as session:
        item = (
            session.query(DailyPlanItem)
            .filter(
                DailyPlanItem.user_id == actor,
                DailyPlanItem.plan_date == plan_date,
                DailyPlanItem.task_id == task_id,
            )
            .one_or_none()
        )
        if item is None:
            return
        skip_plan_item(session, item_id=item.id, actor=actor)
        session.add(
            AuditLog(
                category="daily_plan",
                action="skipped",
                entity_type="task",
                entity_id=str(task_id),
                actor=actor,
                payload={"plan_date": plan_date.isoformat()},
            )
        )

    # Acknowledge in the same DM thread so the user sees the action
    # took effect without us having to update the entire card.
    channel = _channel(body)
    msg_ts = _message_ts(body)
    if channel and msg_ts:
        try:
            sender.post_message(
                channel=channel,
                thread_ts=msg_ts,
                text=f":heavy_minus_sign: Removed task #{task_id} from the plan.",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("plan_skip_ack_failed", error=str(e))


def handle_plan_approve(*, body: dict[str, Any], sender, ack: Ack) -> None:
    """Approve click — symbolic. The plan rows already exist; this
    just records that the user explicitly confirmed."""
    ack()
    actor = _actor(body)
    if not actor:
        return
    raw = (body.get("actions") or [{}])[0].get("value") or ""
    try:
        plan_date = date.fromisoformat(raw)
    except ValueError:
        return

    with session_scope() as session:
        session.add(
            AuditLog(
                category="daily_plan",
                action="approved",
                entity_type="daily_plan",
                # entity_id = plan_date so `_was_explicitly_approved`
                # can match by index without scanning JSON payload.
                entity_id=plan_date.isoformat(),
                actor=actor,
                payload={"plan_date": plan_date.isoformat()},
            )
        )

    channel = _channel(body)
    msg_ts = _message_ts(body)
    if channel and msg_ts:
        try:
            sender.post_message(
                channel=channel,
                thread_ts=msg_ts,
                text=":white_check_mark: Plan accepted. See you in the morning!",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("plan_approve_ack_failed", error=str(e))
