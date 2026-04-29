"""FR-CR-05-41 — Morning task cards (one card per task).

The legacy `send_morning_plan` posted a single bullet-list DM
that doesn't surface the per-task action buttons. The morning
flow operators actually want is one INTERACTIVE CARD per task,
identical to the cards posted on initial intent confirmation:
title hyperlinks to the source message, owner deeplink, full
[Start / Edit / Mark done / Delete / Subscribe] keyboard.

Selector, ordering, and idempotency mirror the FR-CR-05-40
evening report so a re-run is a no-op:

  - One DM with a single «☀ Доброе утро — задачи на день: N»
    intro, listing tasks tersely.
  - Then one task card per open task due today, ordered:
    priority desc → due_time asc → start_time asc → id asc.

Tasks that are owned by the recipient land first; if they're
also subscribed to others' tasks due today, those follow with
a thin separator. Same `task_card_keyboard` permissions as
the live cards: Start only for owner, Edit/Done/Delete for
owner+admin, Subscribe toggle for non-owners.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import (
    AuditLog,
    Task,
    TaskStatus,
    TaskSubscription,
)
from app.telegram_bot.handlers import admin_user_ids
from app.telegram_bot.keyboards import task_card_keyboard
from app.telegram_bot.notifications import (
    _is_telegram_user_id,
    _telegram_owner_ids,
)
from app.telegram_bot.sender import (
    PRIORITY_EMOJI,
    TelegramSender,
    build_task_card_text,
)

log = get_logger(__name__)


_OPEN = (TaskStatus.backlog, TaskStatus.todo, TaskStatus.in_progress)
_CATEGORY = "telegram_morning_cards"


# Priority sort order — urgent tasks first, low last.
_PRIORITY_RANK = {
    "urgent": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}


# --------------------------------------------------------------------------- #
# Audit / idempotency
# --------------------------------------------------------------------------- #


def _already_sent(session: Session, *, user_id: str, day: date) -> bool:
    return (
        session.query(AuditLog)
        .filter(
            AuditLog.category == _CATEGORY,
            AuditLog.actor == user_id,
            AuditLog.entity_id == day.isoformat(),
        )
        .first()
        is not None
    )


def _mark_sent(
    session: Session, *, user_id: str, day: date, payload: dict
) -> None:
    session.add(
        AuditLog(
            category=_CATEGORY,
            action="morning",
            entity_type=_CATEGORY,
            entity_id=day.isoformat(),
            actor=user_id,
            payload=payload,
        )
    )
    session.flush()


# --------------------------------------------------------------------------- #
# Selectors
# --------------------------------------------------------------------------- #


def _owned_due_today(
    session: Session, *, owner_uid: str, today: date
) -> list[Task]:
    """Owner's open tasks that need to land on today's plan.

    A task is «for today» if EITHER:
      - `due_date == today`, OR
      - it's currently `in_progress` (must finish or move it),
        OR
      - it's flagged `is_current_week` AND status in (todo,
        backlog) AND due_date is None (no firm due date but
        slated for this week)."""
    return (
        session.query(Task)
        .filter(
            Task.owner_user_id == owner_uid,
            Task.deleted_at.is_(None),
            Task.status.in_(_OPEN),
            or_(
                Task.due_date == today,
                Task.status == TaskStatus.in_progress,
                (Task.is_current_week.is_(True))
                & Task.due_date.is_(None)
                & Task.status.in_((TaskStatus.todo, TaskStatus.backlog)),
            ),
        )
        .all()
    )


def _subscribed_due_today(
    session: Session, *, recipient_uid: str, today: date
) -> list[Task]:
    """Open tasks the user follows that are due today (or
    already in flight)."""
    return (
        session.query(Task)
        .join(TaskSubscription, TaskSubscription.task_id == Task.id)
        .filter(
            TaskSubscription.slack_user_id == recipient_uid,
            Task.deleted_at.is_(None),
            Task.status.in_(_OPEN),
            (Task.owner_user_id != recipient_uid) | (Task.owner_user_id.is_(None)),
            or_(
                Task.due_date == today,
                Task.status == TaskStatus.in_progress,
            ),
        )
        .distinct()
        .all()
    )


def _sort_tasks_for_morning(tasks: list[Task]) -> list[Task]:
    """priority desc → due_time asc → start_time asc → id asc."""

    def key(t: Task):
        pri_rank = _PRIORITY_RANK.get(t.priority.value, 99)
        # `time` instances sort fine, but None has to go last.
        due_t = t.due_time or time(23, 59, 59)
        start_t = t.start_time or time(23, 59, 59)
        return (pri_rank, due_t, start_t, t.id)

    return sorted(tasks, key=key)


# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #


def _build_intro_text(*, today: date, tasks: list[Task]) -> str:
    if not tasks:
        return f"☀ <b>Доброе утро — на сегодня {today.isoformat()}</b>\nПусто. Хорошего дня."
    lines = [
        f"☀ <b>Доброе утро — задачи на {today.isoformat()}: {len(tasks)}</b>",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


@dataclass
class MorningCardsReport:
    recipients: int = 0
    cards_sent: int = 0
    skipped_idempotent: int = 0
    skipped_no_tasks: int = 0
    failures: int = 0


def send_morning_task_cards(
    session: Session,
    *,
    sender: TelegramSender,
    today: date | None = None,
) -> MorningCardsReport:
    """Send one interactive task card per due-today task to every
    Telegram owner / subscriber. Idempotent per (user, date)."""
    today = today or date.today()
    report = MorningCardsReport()

    owners = _telegram_owner_ids(session)
    sub_only = _telegram_subscriber_ids(session)
    admin_uids = sorted(admin_user_ids())
    recipients = sorted(set(owners) | set(sub_only))

    for uid in recipients:
        if _already_sent(session, user_id=uid, day=today):
            report.skipped_idempotent += 1
            continue
        owned = _sort_tasks_for_morning(
            _owned_due_today(session, owner_uid=uid, today=today)
        )
        subs = _sort_tasks_for_morning(
            _subscribed_due_today(session, recipient_uid=uid, today=today)
        )
        all_tasks = owned + subs
        if not all_tasks:
            report.skipped_no_tasks += 1
            _mark_sent(
                session, user_id=uid, day=today,
                payload={"cards": 0, "owned": 0, "subs": 0},
            )
            continue

        # Intro DM listing the day's load at a glance.
        try:
            sender.send_message(
                chat_id=int(uid),
                text=_build_intro_text(today=today, tasks=all_tasks),
            )
        except Exception as e:  # noqa: BLE001
            report.failures += 1
            log.warning("morning_cards_intro_send_failed", uid=uid, error=str(e))
            continue
        cards = 0
        is_admin = uid in admin_uids
        for t in owned:
            if _post_one_card(
                sender=sender,
                session=session,
                chat_id=int(uid),
                task=t,
                is_owner=True,
                is_admin=is_admin,
            ):
                cards += 1
        # Tasks the user follows (separator first if there were
        # owned ones above).
        if subs and owned:
            try:
                sender.send_message(
                    chat_id=int(uid),
                    text="— — —\n👀 <b>Подписки</b>",
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "morning_cards_subs_separator_failed",
                    uid=uid,
                    error=str(e),
                )
        for t in subs:
            subscribed = True
            if _post_one_card(
                sender=sender,
                session=session,
                chat_id=int(uid),
                task=t,
                is_owner=(t.owner_user_id == uid),
                is_admin=is_admin,
                subscribed=subscribed,
            ):
                cards += 1

        if cards == 0:
            report.failures += 1
            continue
        _mark_sent(
            session, user_id=uid, day=today,
            payload={"cards": cards, "owned": len(owned), "subs": len(subs)},
        )
        report.recipients += 1
        report.cards_sent += cards
    return report


def _post_one_card(
    *,
    sender: TelegramSender,
    session: Session,
    chat_id: int,
    task: Task,
    is_owner: bool,
    is_admin: bool,
    subscribed: bool = False,
) -> bool:
    """Render + send one task card with the same keyboard the
    live cards use. Returns True on success. Failures are logged
    but don't abort the rest of the digest."""
    text = build_task_card_text(task, session=session)
    keyboard = task_card_keyboard(
        task_id=task.id,
        status=task.status.value,
        is_owner=is_owner,
        is_admin=is_admin,
        subscribed=subscribed,
    )
    try:
        sender.send_message(
            chat_id=chat_id, text=text, reply_markup=keyboard
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "morning_cards_card_send_failed",
            chat_id=chat_id,
            task_id=task.id,
            error=str(e),
        )
        return False
    return True


def _telegram_subscriber_ids(session: Session) -> list[str]:
    """Distinct Telegram user ids that subscribe to at least one
    open, non-deleted task. Mirrors the helper in
    `evening_status` but lives here too so the morning module is
    importable on its own."""
    rows = (
        session.query(TaskSubscription.slack_user_id)
        .join(Task, Task.id == TaskSubscription.task_id)
        .filter(
            Task.deleted_at.is_(None),
            Task.status.in_(_OPEN),
        )
        .distinct()
        .all()
    )
    return sorted({r[0] for r in rows if _is_telegram_user_id(r[0])})


__all__ = [
    "MorningCardsReport",
    "send_morning_task_cards",
]
