"""Daily plan workflow.

Two cron jobs per day per user:

  evening (default 18:00 London) → plan-evening
    Inserts one daily_plan_items row per candidate task for the next
    day, then DMs the user a list with "Skip" buttons + a single
    "Approve" button. The user can drop tasks they won't do; whatever
    is left after Skip clicks is the morning plan.

  morning (default 09:00 London) → plan-morning
    Reads the items left from yesterday's plan, posts a DM with each
    task as a card + Start button, and a Tracking section listing
    every task the user is subscribed to (the "favourites").

Idempotency: every send writes an audit_logs row keyed by
(category=daily_plan, action={evening,morning}, payload.user, day).
Re-running the cron on the same day for the same user is a no-op.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import (
    AuditLog,
    DailyPlanItem,
    Task,
    TaskStatus,
    TaskSubscription,
)
from app.slack_bot import blocks as bk

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Selection logic
# --------------------------------------------------------------------------- #

# Statuses that count as "still to do".
_OPEN_STATUSES = (TaskStatus.backlog, TaskStatus.todo, TaskStatus.in_progress, TaskStatus.review)


def _candidate_tasks_for(session: Session, user_id: str, plan_date: date) -> list[Task]:
    """A task lands in tomorrow's plan if it belongs to this user and
    matches at least one of:

    - start_date == plan_date
    - due_date == plan_date
    - is_current_week is True AND status in (todo, in_progress)
    """
    q = (
        session.query(Task)
        .filter(Task.owner_user_id == user_id)
        .filter(Task.status.in_(_OPEN_STATUSES))
        .filter(
            or_(
                Task.start_date == plan_date,
                Task.due_date == plan_date,
                (Task.is_current_week.is_(True))
                & Task.status.in_((TaskStatus.todo, TaskStatus.in_progress)),
            )
        )
        .order_by(Task.due_date.asc().nullslast(), Task.id.asc())
    )
    return q.all()


def _subscribed_tracking(session: Session, user_id: str) -> list[Task]:
    """Tasks the user subscribes to but does NOT own — the "favourites"
    Tracking section."""
    return (
        session.query(Task)
        .join(TaskSubscription, TaskSubscription.task_id == Task.id)
        .filter(TaskSubscription.slack_user_id == user_id)
        .filter(Task.owner_user_id != user_id)
        .filter(Task.status.in_(_OPEN_STATUSES))
        .order_by(Task.due_date.asc().nullslast(), Task.id.asc())
        .all()
    )


# --------------------------------------------------------------------------- #
# Audit-backed idempotency
# --------------------------------------------------------------------------- #


def _already_sent(session: Session, *, action: str, user_id: str, plan_date: date) -> bool:
    """Idempotency check via audit_logs.

    We store the plan_date in entity_id (and the user in actor) so the
    lookup is a plain index-friendly equality. Querying inside JSON
    payload via .contains() generates `LIKE` on Postgres for type
    JSON, which fails — see commit history for the bug.
    """
    return (
        session.query(AuditLog)
        .filter(
            AuditLog.category == "daily_plan",
            AuditLog.action == action,
            AuditLog.actor == user_id,
            AuditLog.entity_id == plan_date.isoformat(),
        )
        .first()
        is not None
    )


def _mark_sent(session: Session, *, action: str, user_id: str, plan_date: date) -> None:
    session.add(
        AuditLog(
            category="daily_plan",
            action=action,
            entity_type="daily_plan",
            entity_id=plan_date.isoformat(),
            actor=user_id,
            payload={"plan_date": plan_date.isoformat()},
        )
    )


# --------------------------------------------------------------------------- #
# Evening: build plan + send approval DM
# --------------------------------------------------------------------------- #


@dataclass
class PlanReport:
    sent: int = 0
    skipped_idempotent: int = 0
    no_tasks: int = 0


def send_evening_plan(
    session: Session,
    *,
    sender,
    plan_date: date,
    user_ids: Iterable[str],
) -> PlanReport:
    """Evening run. For each user_id, persist a plan_date row per
    candidate task and DM them the approval card."""
    report = PlanReport()
    for user_id in user_ids:
        if _already_sent(
            session, action="evening_sent", user_id=user_id, plan_date=plan_date
        ):
            report.skipped_idempotent += 1
            continue

        candidates = _candidate_tasks_for(session, user_id, plan_date)
        # Persist plan rows up-front (excluded_at NULL by default), so
        # the morning run can read them even if the user never clicks
        # Approve.
        existing = {
            (i.user_id, i.plan_date, i.task_id): i
            for i in (
                session.query(DailyPlanItem)
                .filter(
                    DailyPlanItem.user_id == user_id,
                    DailyPlanItem.plan_date == plan_date,
                )
                .all()
            )
        }
        for t in candidates:
            key = (user_id, plan_date, t.id)
            if key not in existing:
                session.add(
                    DailyPlanItem(
                        user_id=user_id, plan_date=plan_date, task_id=t.id
                    )
                )
        session.flush()

        if not candidates:
            report.no_tasks += 1
            _mark_sent(
                session, action="evening_sent", user_id=user_id, plan_date=plan_date
            )
            continue

        tracking = _subscribed_tracking(session, user_id)
        blocks = _evening_blocks(
            plan_date=plan_date, tasks=candidates, tracking=tracking
        )
        try:
            sender.post_message(
                channel=user_id,
                blocks=blocks,
                text=f":calendar: Plan for {plan_date.isoformat()}",
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "daily_plan_evening_send_failed", user_id=user_id, error=str(e)
            )
            continue

        _mark_sent(
            session, action="evening_sent", user_id=user_id, plan_date=plan_date
        )
        report.sent += 1
    return report


def send_morning_plan(
    session: Session,
    *,
    sender,
    plan_date: date,
    user_ids: Iterable[str],
) -> PlanReport:
    """Morning run. Read the surviving plan items and DM the user
    actionable cards + their tracking list."""
    report = PlanReport()
    for user_id in user_ids:
        if _already_sent(
            session, action="morning_sent", user_id=user_id, plan_date=plan_date
        ):
            report.skipped_idempotent += 1
            continue

        items = (
            session.query(DailyPlanItem)
            .filter(
                DailyPlanItem.user_id == user_id,
                DailyPlanItem.plan_date == plan_date,
                DailyPlanItem.excluded_at.is_(None),
            )
            .all()
        )
        if not items:
            report.no_tasks += 1
            _mark_sent(
                session, action="morning_sent", user_id=user_id, plan_date=plan_date
            )
            continue

        tasks = (
            session.query(Task)
            .filter(Task.id.in_([i.task_id for i in items]))
            .filter(Task.status.in_(_OPEN_STATUSES))
            .order_by(Task.due_date.asc().nullslast(), Task.id.asc())
            .all()
        )
        tracking = _subscribed_tracking(session, user_id)

        try:
            sender.post_message(
                channel=user_id,
                blocks=_morning_blocks(
                    plan_date=plan_date,
                    user_id=user_id,
                    tasks=tasks,
                    tracking=tracking,
                ),
                text=f":sunny: Today's plan — {plan_date.isoformat()}",
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "daily_plan_morning_send_failed", user_id=user_id, error=str(e)
            )
            continue

        _mark_sent(
            session, action="morning_sent", user_id=user_id, plan_date=plan_date
        )
        report.sent += 1
    return report


# --------------------------------------------------------------------------- #
# Skip handling (button on the evening card)
# --------------------------------------------------------------------------- #


def skip_plan_item(session: Session, *, item_id: int, actor: str) -> DailyPlanItem | None:
    """Mark an item excluded — flips it out of tomorrow's plan."""
    item = session.get(DailyPlanItem, item_id)
    if item is None:
        return None
    if item.user_id != actor:
        # A user can only skip their own items.
        return None
    if item.excluded_at is None:
        item.excluded_at = datetime.now(timezone.utc)
    return item


# --------------------------------------------------------------------------- #
# Block builders (kept here since they're plan-specific)
# --------------------------------------------------------------------------- #


def _task_summary_line(task: Task, *, with_status: bool = False) -> str:
    parts: list[str] = [f"*#{task.id}* {task.title}"]
    if with_status:
        parts.append(f"`{task.status.value}`")
    if task.due_date:
        parts.append(f"due {task.due_date.isoformat()}")
    if task.owner_user_id:
        parts.append(f"<@{task.owner_user_id}>")
    return " · ".join(parts)


def _evening_blocks(
    *,
    plan_date: date,
    tasks: list[Task],
    tracking: list[Task],
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f":calendar: Plan for {plan_date.isoformat()}",
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        "Your plan for tomorrow. Drop tasks you won't get to "
                        "with *Skip*. When you're ready — *Approve plan*."
                    ),
                }
            ],
        },
        {"type": "divider"},
    ]
    for t in tasks:
        # Sender will look up the matching DailyPlanItem id by
        # (user, plan_date, task_id) — but we don't have user here, so
        # we use task_id as the action value and resolve the row
        # server-side.
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": _task_summary_line(t)},
                "accessory": {
                    "type": "button",
                    "action_id": bk.ACTION_PLAN_SKIP,
                    "text": {"type": "plain_text", "text": "Skip"},
                    "value": f"{plan_date.isoformat()}|{t.id}",
                },
            }
        )

    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "action_id": bk.ACTION_PLAN_APPROVE,
                    "text": {"type": "plain_text", "text": "Approve plan"},
                    "value": plan_date.isoformat(),
                }
            ],
        }
    )

    if tracking:
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":star: *Tracking ({len(tracking)})*",
                },
            }
        )
        for t in tracking:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": _task_summary_line(t, with_status=True),
                        }
                    ],
                }
            )
    return blocks


def _morning_blocks(
    *,
    plan_date: date,
    user_id: str,
    tasks: list[Task],
    tracking: list[Task],
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f":sunny: Today — {plan_date.isoformat()}",
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        "Click *Start* on a task when you begin it. "
                        "Close it with *Mark done* on the card."
                    ),
                }
            ],
        },
        {"type": "divider"},
    ]
    for t in tasks:
        # Re-use the existing task_card so the morning DM looks the
        # same as cards everywhere else in the bot.
        blocks.extend(bk.task_card(task=t, viewer_slack_user_id=user_id))
        blocks.append({"type": "divider"})

    if tracking:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":star: *Tracking ({len(tracking)})*",
                },
            }
        )
        for t in tracking:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": _task_summary_line(t, with_status=True),
                        }
                    ],
                }
            )
    return blocks


__all__ = [
    "PlanReport",
    "send_evening_plan",
    "send_morning_plan",
    "skip_plan_item",
]
