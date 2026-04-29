"""Telegram-side digests, plans, and reminders (FR-CR-04-29).

Mirrors the Slack notification surface (`app/services/digest.py`,
`app/services/daily_plan.py`, `app/services/weekly_plan.py`,
`app/services/admin_digest.py`, `app/services/thread_reminders.py`)
for users who own / subscribe to / live in Telegram-source tasks
and chats.

What's the same:

- The same per-user idempotency keys in ``audit_logs`` — running
  the cron twice in a row is a no-op. We use a separate `category`
  prefix (``telegram_*``) so Slack and Telegram digests don't
  shadow each other.
- The same selection logic for "open tasks", "tracking section",
  "tomorrow / overdue", etc. Telegram users see the same content,
  formatted with Telegram emoji rather than Slack ones.

What's different:

- We DM Telegram users by their numeric user_id (which equals the
  private-chat id once they've started a conversation with the
  bot). For users who never started the bot, sends fail silently
  with a logged warning — the bot can't initiate a private
  conversation.
- Thread reminders go into the source group chat (``Task.source_
  conversation_id``) when the task came from Telegram, instead of
  the Slack source thread.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import (
    AuditLog,
    DailyPlanItem,
    Task,
    TaskSourceKind,
    TaskStatus,
    TaskSubscription,
)
from app.telegram_bot.handlers import admin_user_ids
from app.telegram_bot.sender import (
    PRIORITY_EMOJI,
    STATUS_EMOJI,
    TelegramSender,
)

log = get_logger(__name__)


_OPEN = (TaskStatus.backlog, TaskStatus.todo, TaskStatus.in_progress)


# --------------------------------------------------------------------------- #
# Common helpers
# --------------------------------------------------------------------------- #


def _is_telegram_user_id(uid: str | None) -> bool:
    """Heuristic: numeric → Telegram, anything else (e.g. starts
    with U / W) → Slack. Used to filter the recipient set so a
    Slack subscriber never gets a Telegram DM (and vice-versa)."""
    if not uid:
        return False
    return uid.lstrip("-").isdigit()


def _telegram_owner_ids(session: Session) -> list[str]:
    """Distinct Telegram user ids that own at least one open,
    non-deleted task."""
    rows = (
        session.query(Task.owner_user_id)
        .filter(
            Task.owner_user_id.isnot(None),
            Task.status.in_(_OPEN),
            Task.deleted_at.is_(None),
        )
        .distinct()
        .all()
    )
    return sorted({r[0] for r in rows if _is_telegram_user_id(r[0])})


def _already_sent(
    session: Session, *, category: str, action: str, user_id: str, day: date
) -> bool:
    return (
        session.query(AuditLog)
        .filter(
            AuditLog.category == category,
            AuditLog.action == action,
            AuditLog.actor == user_id,
            AuditLog.entity_id == day.isoformat(),
        )
        .first()
        is not None
    )


def _mark_sent(
    session: Session, *, category: str, action: str, user_id: str, day: date
) -> None:
    session.add(
        AuditLog(
            category=category,
            action=action,
            entity_type=category,
            entity_id=day.isoformat(),
            actor=user_id,
            payload={"day": day.isoformat()},
        )
    )
    session.flush()


def _fmt_task_line(task: Task) -> str:
    parts = [f"*#{task.id}* {task.title}"]
    parts.append(f"{STATUS_EMOJI.get(task.status.value, '')} `{task.status.value}`")
    if task.due_date:
        parts.append(f"📅 {task.due_date.isoformat()}")
    pri_em = PRIORITY_EMOJI.get(task.priority.value, "")
    parts.append(f"{pri_em} {task.priority.value}")
    if task.owner_user_id:
        parts.append(f"👤 {task.owner_user_id}")
    return "• " + " · ".join(parts)


@dataclass
class TelegramDigestReport:
    recipients: int = 0
    skipped_idempotent: int = 0
    skipped_no_tasks: int = 0
    failures: int = 0


# --------------------------------------------------------------------------- #
# Morning digest — Today / Approaching deadlines / Overdue
# --------------------------------------------------------------------------- #


def send_morning_digest(
    session: Session,
    *,
    sender: TelegramSender,
    today: date | None = None,
) -> TelegramDigestReport:
    """FR-CR-05-01 — morning DM at 09:00 narrows to «Today's tasks»
    only. Approaching / Overdue moved to dedicated channels (per-task
    deadline reminders + the evening 3-section DM)."""
    today = today or date.today()
    report = TelegramDigestReport()

    for uid in _telegram_owner_ids(session):
        if _already_sent(
            session,
            category="telegram_digest",
            action="morning",
            user_id=uid,
            day=today,
        ):
            report.skipped_idempotent += 1
            continue

        today_tasks = (
            session.query(Task)
            .filter(
                Task.owner_user_id == uid,
                Task.status.in_(_OPEN),
                Task.deleted_at.is_(None),
                Task.due_date == today,
            )
            .order_by(Task.id)
            .all()
        )

        if not today_tasks:
            report.skipped_no_tasks += 1
            _mark_sent(
                session,
                category="telegram_digest",
                action="morning",
                user_id=uid,
                day=today,
            )
            continue

        body = (
            f"<b>📅 Today — {today.isoformat()}</b>\n"
            + "\n".join(_fmt_task_line(t) for t in today_tasks)
        )
        try:
            sender.send_message(chat_id=int(uid), text=body)
        except Exception as e:  # noqa: BLE001
            report.failures += 1
            log.warning("telegram_morning_digest_send_failed", uid=uid, error=str(e))
            continue
        _mark_sent(
            session,
            category="telegram_digest",
            action="morning",
            user_id=uid,
            day=today,
        )
        report.recipients += 1
    return report


# --------------------------------------------------------------------------- #
# Daily plan (evening triage + morning execution)
# --------------------------------------------------------------------------- #


def _candidate_tasks_for(session: Session, user_id: str, plan_date: date) -> list[Task]:
    return (
        session.query(Task)
        .filter(Task.owner_user_id == user_id)
        .filter(Task.status.in_(_OPEN))
        .filter(Task.deleted_at.is_(None))
        .filter(
            or_(
                Task.start_date == plan_date,
                Task.due_date == plan_date,
                (Task.is_current_week.is_(True))
                & Task.status.in_((TaskStatus.todo, TaskStatus.in_progress)),
            )
        )
        .order_by(Task.due_date.asc().nullslast(), Task.id.asc())
        .all()
    )


def send_evening_plan(
    session: Session,
    *,
    sender: TelegramSender,
    plan_date: date,
) -> TelegramDigestReport:
    """The Telegram analogue of the Slack evening-plan DM. We
    don't expose Skip/Approve buttons here for MVP — we just send
    tomorrow's plan as a heads-up. The morning execution path
    runs as-is regardless of approve clicks (FR-CR-04-25)."""
    report = TelegramDigestReport()
    for uid in _telegram_owner_ids(session):
        if _already_sent(
            session,
            category="telegram_plan",
            action="evening",
            user_id=uid,
            day=plan_date,
        ):
            report.skipped_idempotent += 1
            continue

        candidates = _candidate_tasks_for(session, uid, plan_date)
        # Always persist plan rows so the morning run can find them.
        existing = {
            (i.user_id, i.plan_date, i.task_id)
            for i in session.query(DailyPlanItem)
            .filter(
                DailyPlanItem.user_id == uid,
                DailyPlanItem.plan_date == plan_date,
            )
            .all()
        }
        for t in candidates:
            key = (uid, plan_date, t.id)
            if key not in existing:
                session.add(
                    DailyPlanItem(user_id=uid, plan_date=plan_date, task_id=t.id)
                )
        session.flush()

        if not candidates:
            report.skipped_no_tasks += 1
            _mark_sent(
                session,
                category="telegram_plan",
                action="evening",
                user_id=uid,
                day=plan_date,
            )
            continue

        body = (
            f"*📅 Plan for {plan_date.isoformat()}*\n\n"
            + "\n".join(_fmt_task_line(t) for t in candidates)
            + "\n\n_We'll run this as-is in the morning._"
        )
        try:
            sender.send_message(chat_id=int(uid), text=body)
        except Exception as e:  # noqa: BLE001
            report.failures += 1
            log.warning("telegram_evening_plan_send_failed", uid=uid, error=str(e))
            continue
        _mark_sent(
            session,
            category="telegram_plan",
            action="evening",
            user_id=uid,
            day=plan_date,
        )
        report.recipients += 1
    return report


def send_morning_plan(
    session: Session,
    *,
    sender: TelegramSender,
    plan_date: date,
) -> TelegramDigestReport:
    report = TelegramDigestReport()
    for uid in _telegram_owner_ids(session):
        if _already_sent(
            session,
            category="telegram_plan",
            action="morning",
            user_id=uid,
            day=plan_date,
        ):
            report.skipped_idempotent += 1
            continue

        items = (
            session.query(DailyPlanItem)
            .filter(
                DailyPlanItem.user_id == uid,
                DailyPlanItem.plan_date == plan_date,
                DailyPlanItem.excluded_at.is_(None),
            )
            .all()
        )
        if not items:
            report.skipped_no_tasks += 1
            _mark_sent(
                session,
                category="telegram_plan",
                action="morning",
                user_id=uid,
                day=plan_date,
            )
            continue

        tasks = (
            session.query(Task)
            .filter(Task.id.in_([i.task_id for i in items]))
            .filter(Task.status.in_(_OPEN))
            .filter(Task.deleted_at.is_(None))
            .order_by(Task.due_date.asc().nullslast(), Task.id.asc())
            .all()
        )
        if not tasks:
            report.skipped_no_tasks += 1
            _mark_sent(
                session,
                category="telegram_plan",
                action="morning",
                user_id=uid,
                day=plan_date,
            )
            continue

        body = (
            f"*☀ Today — {plan_date.isoformat()}*\n\n"
            + "\n".join(_fmt_task_line(t) for t in tasks)
        )
        try:
            sender.send_message(chat_id=int(uid), text=body)
        except Exception as e:  # noqa: BLE001
            report.failures += 1
            log.warning("telegram_morning_plan_send_failed", uid=uid, error=str(e))
            continue
        _mark_sent(
            session,
            category="telegram_plan",
            action="morning",
            user_id=uid,
            day=plan_date,
        )
        report.recipients += 1
    return report


# --------------------------------------------------------------------------- #
# Weekly plan (Sunday)
# --------------------------------------------------------------------------- #


def send_weekly_plan(
    session: Session,
    *,
    sender: TelegramSender,
    today: date | None = None,
) -> TelegramDigestReport:
    today = today or date.today()
    week_start = today + timedelta(days=(7 - today.weekday()) % 7)  # next Monday
    week_end = week_start + timedelta(days=6)
    report = TelegramDigestReport()

    for uid in _telegram_owner_ids(session):
        if _already_sent(
            session,
            category="telegram_plan",
            action="weekly",
            user_id=uid,
            day=today,
        ):
            report.skipped_idempotent += 1
            continue

        tasks = (
            session.query(Task)
            .filter(
                Task.owner_user_id == uid,
                Task.status == TaskStatus.backlog,
                Task.deleted_at.is_(None),
                Task.due_date.isnot(None),
                Task.due_date >= week_start,
                Task.due_date <= week_end,
            )
            .order_by(Task.due_date, Task.id)
            .all()
        )
        if not tasks:
            report.skipped_no_tasks += 1
            _mark_sent(
                session,
                category="telegram_plan",
                action="weekly",
                user_id=uid,
                day=today,
            )
            continue

        body = (
            f"*🗓 Week ahead — {week_start.isoformat()}…{week_end.isoformat()}*\n\n"
            + "\n".join(_fmt_task_line(t) for t in tasks)
        )
        try:
            sender.send_message(chat_id=int(uid), text=body)
        except Exception as e:  # noqa: BLE001
            report.failures += 1
            log.warning("telegram_weekly_plan_send_failed", uid=uid, error=str(e))
            continue
        _mark_sent(
            session,
            category="telegram_plan",
            action="weekly",
            user_id=uid,
            day=today,
        )
        report.recipients += 1
    return report


# --------------------------------------------------------------------------- #
# Deadline reminders (per-task, dedup per (task, day))
# --------------------------------------------------------------------------- #


def send_starts_now(
    session: Session,
    *,
    sender: TelegramSender,
    today: date | None = None,
) -> TelegramDigestReport:
    """FR-CR-05-03 — DM Telegram task owners when a Task's
    ``start_time`` reaches now (±5 min). Mirrors the Slack
    `_starts_now` selection. No `start_time` ⇒ defaults to 09:00."""
    from datetime import datetime, time as _time

    today = today or date.today()
    now = datetime.now()
    window_start = now - timedelta(minutes=5)
    report = TelegramDigestReport()

    candidates = (
        session.query(Task)
        .filter(
            Task.status.in_(_OPEN),
            Task.deleted_at.is_(None),
            Task.start_date == today,
        )
        .all()
    )
    for t in candidates:
        if not _is_telegram_user_id(t.owner_user_id):
            continue
        start_t = t.start_time or _time(9, 0)
        start_dt = datetime.combine(today, start_t)
        if not (window_start <= start_dt <= now):
            continue
        action_key = (
            f"start:{t.id}:{today.isoformat()}:{start_t.strftime('%H%M')}"
        )
        already = (
            session.query(AuditLog)
            .filter(
                AuditLog.category == "telegram_digest",
                AuditLog.action == action_key,
            )
            .first()
        )
        if already is not None:
            report.skipped_idempotent += 1
            continue
        try:
            sender.send_message(
                chat_id=int(t.owner_user_id),
                text=(
                    f"🚀 <b>Starting now</b> — <b>#{t.id} {t.title}</b> "
                    f"(scheduled {start_t.strftime('%H:%M')})"
                ),
            )
        except Exception as e:  # noqa: BLE001
            report.failures += 1
            log.warning("telegram_starts_now_send_failed", task_id=t.id, error=str(e))
            continue
        session.add(
            AuditLog(
                category="telegram_digest",
                action=action_key,
                entity_type="task",
                entity_id=str(t.id),
                actor=t.owner_user_id,
                payload={"start_time": start_t.strftime("%H:%M")},
            )
        )
        report.recipients += 1
    return report


def send_deadline_reminders(
    session: Session,
    *,
    sender: TelegramSender,
    today: date | None = None,
) -> TelegramDigestReport:
    today = today or date.today()
    soon = today + timedelta(days=2)
    report = TelegramDigestReport()

    tasks = (
        session.query(Task)
        .filter(
            Task.status.in_(_OPEN),
            Task.deleted_at.is_(None),
            Task.due_date.isnot(None),
            Task.due_date <= soon,
        )
        .all()
    )
    for t in tasks:
        if not _is_telegram_user_id(t.owner_user_id):
            continue
        action_key = f"deadline:{t.id}:{today.isoformat()}"
        already = (
            session.query(AuditLog)
            .filter(
                AuditLog.category == "telegram_digest",
                AuditLog.action == action_key,
            )
            .first()
        )
        if already is not None:
            report.skipped_idempotent += 1
            continue
        overdue = t.due_date < today
        label = "⚠ Overdue" if overdue else "⏰ Approaching"
        text = f"{label}: *#{t.id}* {t.title} — due {t.due_date.isoformat()}"
        try:
            sender.send_message(chat_id=int(t.owner_user_id), text=text)
        except Exception as e:  # noqa: BLE001
            report.failures += 1
            log.warning(
                "telegram_deadline_reminder_send_failed",
                task_id=t.id,
                error=str(e),
            )
            continue
        session.add(
            AuditLog(
                category="telegram_digest",
                action=action_key,
                entity_type="task",
                entity_id=str(t.id),
                actor=t.owner_user_id,
                payload={"day": today.isoformat(), "overdue": overdue},
            )
        )
        report.recipients += 1
    return report


# --------------------------------------------------------------------------- #
# Thread reminders (group chats, per (task, day))
# --------------------------------------------------------------------------- #


def send_thread_reminders(
    session: Session,
    *,
    sender: TelegramSender,
    today: date | None = None,
) -> TelegramDigestReport:
    """Daily nudge in the source Telegram chat for each open
    Telegram-sourced task. Mirrors the Slack thread-reminder
    behaviour."""
    today = today or date.today()
    week_end = today + timedelta(days=(6 - today.weekday()))
    report = TelegramDigestReport()

    tasks = (
        session.query(Task)
        .filter(
            Task.source_kind == TaskSourceKind.telegram,
            Task.status.in_(_OPEN),
            Task.deleted_at.is_(None),
        )
        .order_by(Task.id)
        .all()
    )
    for t in tasks:
        chat_id = t.source_conversation_id
        if not chat_id:
            continue
        action_key = f"thread_reminder:{t.id}:{today.isoformat()}"
        already = (
            session.query(AuditLog)
            .filter(
                AuditLog.category == "telegram_thread_reminder",
                AuditLog.action == action_key,
            )
            .first()
        )
        if already is not None:
            report.skipped_idempotent += 1
            continue
        if t.status == TaskStatus.in_progress:
            text = f"🛠 task *#{t.id}* — what's the progress?"
        elif t.status == TaskStatus.todo and t.due_date and t.due_date <= week_end:
            text = (
                f"📌 this week: *{t.title}*"
                + (f" — by {t.due_date.isoformat()}" if t.due_date else "")
            )
        elif t.status == TaskStatus.backlog and t.due_date and t.due_date <= week_end:
            text = (
                f"📥 *{t.title}* waiting to start"
                + (f" — by {t.due_date.isoformat()}" if t.due_date else "")
            )
        else:
            continue
        # Reply under the source message when we have it.
        reply_to = None
        try:
            reply_to = int(t.source_message_ts) if t.source_message_ts else None
        except (TypeError, ValueError):
            reply_to = None
        try:
            sender.send_message(
                chat_id=int(chat_id),
                text=text,
                reply_to_message_id=reply_to,
            )
        except Exception as e:  # noqa: BLE001
            report.failures += 1
            log.warning(
                "telegram_thread_reminder_send_failed",
                task_id=t.id,
                error=str(e),
            )
            continue
        session.add(
            AuditLog(
                category="telegram_thread_reminder",
                action=action_key,
                entity_type="task",
                entity_id=str(t.id),
                payload={"day": today.isoformat()},
            )
        )
        report.recipients += 1
    return report


# --------------------------------------------------------------------------- #
# Admin watch-list
# --------------------------------------------------------------------------- #


def send_admin_watchlist(
    session: Session,
    *,
    sender: TelegramSender,
    today: date | None = None,
) -> TelegramDigestReport:
    today = today or date.today()
    report = TelegramDigestReport()

    admins = admin_user_ids()
    if not admins:
        return report

    in_progress = (
        session.query(Task)
        .filter(
            Task.status == TaskStatus.in_progress,
            Task.deleted_at.is_(None),
        )
        .order_by(Task.id)
        .all()
    )
    overdue = (
        session.query(Task)
        .filter(
            Task.status.in_(_OPEN),
            Task.deleted_at.is_(None),
            Task.due_date.isnot(None),
            Task.due_date < today,
        )
        .order_by(Task.due_date)
        .all()
    )

    for admin_id in sorted(admins):
        if _already_sent(
            session,
            category="telegram_admin_digest",
            action="morning",
            user_id=admin_id,
            day=today,
        ):
            report.skipped_idempotent += 1
            continue
        body = (
            f"*👀 Watch-list — {today.isoformat()}*\n\n"
            f"*In progress* ({len(in_progress)})\n"
            + ("\n".join(_fmt_task_line(t) for t in in_progress) or "_(none)_")
            + "\n\n*Overdue* ("
            + str(len(overdue))
            + ")\n"
            + ("\n".join(_fmt_task_line(t) for t in overdue) or "_(none)_")
        )
        try:
            sender.send_message(chat_id=int(admin_id), text=body)
        except Exception as e:  # noqa: BLE001
            report.failures += 1
            log.warning("telegram_admin_watchlist_send_failed", admin=admin_id, error=str(e))
            continue
        _mark_sent(
            session,
            category="telegram_admin_digest",
            action="morning",
            user_id=admin_id,
            day=today,
        )
        report.recipients += 1
    return report


__all__ = [
    "TelegramDigestReport",
    "send_morning_digest",
    "send_evening_plan",
    "send_morning_plan",
    "send_weekly_plan",
    "send_deadline_reminders",
    "send_thread_reminders",
    "send_admin_watchlist",
]
