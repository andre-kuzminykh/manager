"""Send daily / weekly / deadlines digest (FR-CR-6).

Usage:
    python -m ops.send_digest --type daily
    python -m ops.send_digest --type weekly
    python -m ops.send_digest --type deadlines
    python -m ops.send_digest --type plan-evening   # 18:00 London
    python -m ops.send_digest --type plan-morning   # 09:00 London

Intended to be invoked by cron / Cloud Scheduler. Idempotency is handled by
`DigestService` via `audit_logs` so repeat runs on the same day are no-ops.
"""
from __future__ import annotations

import argparse
import sys

from slack_sdk import WebClient

from app.config import get_settings
from app.db import session_scope
from app.logging_setup import get_logger, setup_logging
from app.services import (
    DigestKind,
    DigestService,
    send_admin_evening_digest,
    send_admin_morning_watch,
    send_thread_reminders,
    send_weekly_plan,
)
from app.services.daily_plan import send_evening_plan, send_morning_plan
from app.slack_bot.rate_limiter import RateAwareSlackSender

log = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--type",
        required=True,
        choices=[k.value for k in DigestKind]
        + [
            "weekly-plan",
            "admin-evening",
            "admin-morning",
            "thread-reminders",
            "plan-evening",
            "plan-morning",
        ],
        help="Digest to send.",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    if not settings.slack_bot_token:
        log.error("missing_slack_bot_token")
        return 2

    client = WebClient(token=settings.slack_bot_token)
    sender = RateAwareSlackSender(client)

    if args.type == "weekly-plan":
        with session_scope() as session:
            report = send_weekly_plan(session, sender=sender)
        log.info(
            "weekly_plan_sent",
            recipients=report.recipients,
            tasks_included=report.tasks_included,
            skipped_idempotent=report.skipped_idempotent,
        )
        return 0

    if args.type == "admin-evening":
        with session_scope() as session:
            report = send_admin_evening_digest(session, sender=sender)
        log.info(
            "admin_evening_sent",
            recipients=report.recipients,
            tasks_included=report.tasks_included,
            skipped_idempotent=report.skipped_idempotent,
        )
        return 0

    if args.type == "admin-morning":
        with session_scope() as session:
            report = send_admin_morning_watch(session, sender=sender)
        log.info(
            "admin_morning_sent",
            recipients=report.recipients,
            tasks_included=report.tasks_included,
            skipped_idempotent=report.skipped_idempotent,
        )
        return 0

    if args.type in ("plan-evening", "plan-morning"):
        from datetime import date, timedelta

        from app.models import Task

        # Plan-date: tomorrow for evening, today for morning. Run at
        # the user's expected local time so London 18:00 = +0/+1 from
        # UTC works out — cron is expected to fire at the right hour.
        plan_date = date.today() + (
            timedelta(days=1) if args.type == "plan-evening" else timedelta(days=0)
        )

        with session_scope() as session:
            # All distinct task owners with at least one open task —
            # we send the plan to anyone who could plausibly have
            # work tomorrow.
            user_ids = sorted(
                {
                    uid
                    for (uid,) in session.query(Task.owner_user_id)
                    .filter(Task.owner_user_id.isnot(None))
                    .distinct()
                }
            )
            if args.type == "plan-evening":
                report = send_evening_plan(
                    session, sender=sender, plan_date=plan_date, user_ids=user_ids
                )
            else:
                report = send_morning_plan(
                    session, sender=sender, plan_date=plan_date, user_ids=user_ids
                )
        log.info(
            "daily_plan_sent",
            phase=args.type,
            plan_date=plan_date.isoformat(),
            sent=report.sent,
            skipped_idempotent=report.skipped_idempotent,
            no_tasks=report.no_tasks,
        )
        return 0

    if args.type == "thread-reminders":
        with session_scope() as session:
            report = send_thread_reminders(session, sender=sender)
        log.info(
            "thread_reminders_sent",
            reminders=report.reminders,
            skipped_idempotent=report.skipped_idempotent,
            skipped_no_thread=report.skipped_no_thread,
            skipped_not_relevant=report.skipped_not_relevant,
        )
        return 0

    service = DigestService(sender=sender)
    kind = DigestKind(args.type)

    with session_scope() as session:
        report = service.send(session, kind)
    log.info(
        "digest_sent",
        kind=kind.value,
        recipients=report.recipients,
        tasks_included=report.tasks_included,
        skipped_idempotent=report.skipped_idempotent,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
