"""CR-03 Phase E — admin-specific digests.

Morning (09:00): admin watch-list DM listing every open (non-done) task
across the team with owner, status, due.

Evening (20:00): admin preview of tomorrow's work by owner plus a 'stale'
section with in_progress tasks that haven't seen a status reply in
>= ADMIN_STALE_THRESHOLD_DAYS days (default 2).

Both are idempotent per (admin, date) via audit_logs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Protocol

from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import AuditLog, Task, TaskStatus
from app.services.employees import admin_slack_user_ids

log = get_logger(__name__)


class _Sender(Protocol):
    def post_message(self, **kwargs) -> dict: ...


_OPEN = (TaskStatus.backlog, TaskStatus.todo, TaskStatus.in_progress, TaskStatus.review)


@dataclass
class AdminDigestReport:
    recipients: int = 0
    tasks_included: int = 0
    skipped_idempotent: int = 0


def _already_sent(session: Session, *, action: str) -> bool:
    return (
        session.query(AuditLog)
        .filter(AuditLog.category == "admin_digest", AuditLog.action == action)
        .first()
        is not None
    )


def _mark_sent(
    session: Session, *, action: str, admin_id: str, payload: dict
) -> None:
    session.add(
        AuditLog(
            category="admin_digest",
            action=action,
            entity_type="user",
            entity_id=admin_id,
            payload=payload,
        )
    )
    session.flush()


def _fmt_task_line(t: Task) -> str:
    owner = (
        f"<@{t.owner_user_id}>" if t.owner_user_id else (t.owner_display_name or "—")
    )
    parts = [f"*#{t.id}* {t.title}", f"`{t.status.value}`", f"owner {owner}"]
    if t.due_date:
        parts.append(f"due {t.due_date.isoformat()}")
    return "• " + " · ".join(parts)


def _fmt_group(title: str, tasks: list[Task]) -> str:
    body = "\n".join(_fmt_task_line(t) for t in tasks) if tasks else "(пусто)"
    return f"*{title} ({len(tasks)})*\n{body}"


# --------------------------------------------------------------------------- #
# Evening digest
# --------------------------------------------------------------------------- #


def send_admin_evening_digest(
    session: Session,
    *,
    sender: _Sender,
    today: date | None = None,
    stale_threshold_days: int = 2,
) -> AdminDigestReport:
    today = today or date.today()
    tomorrow = today + timedelta(days=1)

    report = AdminDigestReport()
    admins = admin_slack_user_ids()
    if not admins:
        return report

    tomorrow_tasks = (
        session.query(Task)
        .filter(Task.status.in_(_OPEN), Task.due_date == tomorrow)
        .order_by(Task.owner_user_id, Task.id)
        .all()
    )
    stale_cutoff = today - timedelta(days=stale_threshold_days)
    stale = (
        session.query(Task)
        .filter(
            Task.status == TaskStatus.in_progress,
            Task.started_at.isnot(None),
            Task.started_at < stale_cutoff,
        )
        .order_by(Task.id)
        .all()
    )

    for admin_id in sorted(admins):
        action = f"evening:{admin_id}:{today.isoformat()}"
        if _already_sent(session, action=action):
            report.skipped_idempotent += 1
            continue
        body = "\n\n".join(
            [
                f"*Вечерний обзор — {today.isoformat()}*",
                _fmt_group(f"Завтра ({tomorrow.isoformat()})", tomorrow_tasks),
                _fmt_group(f"Стоят >= {stale_threshold_days} дней", stale),
            ]
        )
        try:
            sender.post_message(
                channel=admin_id,
                text="Admin evening digest",
                blocks=[
                    {"type": "section", "text": {"type": "mrkdwn", "text": body}}
                ],
            )
        except Exception as e:  # noqa: BLE001
            log.warning("admin_evening_failed", admin=admin_id, error=str(e))
            continue
        _mark_sent(
            session,
            action=action,
            admin_id=admin_id,
            payload={"tomorrow": len(tomorrow_tasks), "stale": len(stale)},
        )
        report.recipients += 1
        report.tasks_included += len(tomorrow_tasks) + len(stale)
    return report


# --------------------------------------------------------------------------- #
# Morning watch-list
# --------------------------------------------------------------------------- #


def send_admin_morning_watch(
    session: Session,
    *,
    sender: _Sender,
    today: date | None = None,
) -> AdminDigestReport:
    today = today or date.today()
    report = AdminDigestReport()
    admins = admin_slack_user_ids()
    if not admins:
        return report

    in_progress = (
        session.query(Task)
        .filter(Task.status == TaskStatus.in_progress)
        .order_by(Task.id)
        .all()
    )
    review = (
        session.query(Task)
        .filter(Task.status == TaskStatus.review)
        .order_by(Task.id)
        .all()
    )
    overdue = (
        session.query(Task)
        .filter(
            Task.status.in_(_OPEN),
            Task.due_date.isnot(None),
            Task.due_date < today,
        )
        .order_by(Task.due_date)
        .all()
    )

    for admin_id in sorted(admins):
        action = f"morning:{admin_id}:{today.isoformat()}"
        if _already_sent(session, action=action):
            report.skipped_idempotent += 1
            continue
        body = "\n\n".join(
            [
                f"*Watch-list — {today.isoformat()}*",
                _fmt_group("In progress", in_progress),
                _fmt_group("On review", review),
                _fmt_group("Overdue", overdue),
            ]
        )
        try:
            sender.post_message(
                channel=admin_id,
                text="Admin morning watch-list",
                blocks=[
                    {"type": "section", "text": {"type": "mrkdwn", "text": body}}
                ],
            )
        except Exception as e:  # noqa: BLE001
            log.warning("admin_morning_failed", admin=admin_id, error=str(e))
            continue
        _mark_sent(
            session,
            action=action,
            admin_id=admin_id,
            payload={
                "in_progress": len(in_progress),
                "review": len(review),
                "overdue": len(overdue),
            },
        )
        report.recipients += 1
        report.tasks_included += (
            len(in_progress) + len(review) + len(overdue)
        )
    return report
