"""Telegram-side digest / plan / reminder cron CLI (FR-CR-04-29).

Mirrors `ops/send_digest.py` for the Telegram channel — same
schedules, same logical events, but DMs go to Telegram users via
the Bot API instead of Slack.

Usage::

    python -m ops.telegram_digest --type morning-digest
    python -m ops.telegram_digest --type plan-evening
    python -m ops.telegram_digest --type plan-morning
    python -m ops.telegram_digest --type weekly
    python -m ops.telegram_digest --type deadlines
    python -m ops.telegram_digest --type thread-reminders
    python -m ops.telegram_digest --type admin-watchlist

Each call is one-shot: connects, sends, exits. Idempotent via
``audit_logs`` — re-running on the same day is a no-op.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import asdict
from datetime import date

from app.config import get_settings
from app.db import session_scope
from app.logging_setup import get_logger, setup_logging
from app.telegram_bot import notifications as tg_notifications
from app.telegram_bot.sender import TelegramSender

log = get_logger(__name__)


_TYPES = {
    "morning-digest": tg_notifications.send_morning_digest,
    "plan-evening": tg_notifications.send_evening_plan,
    "plan-morning": tg_notifications.send_morning_plan,
    "weekly": tg_notifications.send_weekly_plan,
    "deadlines": tg_notifications.send_deadline_reminders,
    "starts-now": tg_notifications.send_starts_now,  # FR-CR-05-03
    "thread-reminders": tg_notifications.send_thread_reminders,
    "admin-watchlist": tg_notifications.send_admin_watchlist,
}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Telegram digests and reminders.")
    p.add_argument("--type", required=True, choices=sorted(_TYPES.keys()))
    p.add_argument(
        "--date",
        type=str,
        default=None,
        help="Override today's date (YYYY-MM-DD). For testing.",
    )
    return p.parse_args()


def main() -> int:
    setup_logging()
    args = _parse_args()
    settings = get_settings()
    if not settings.telegram_bot_token:
        log.error("missing_telegram_bot_token")
        return 2

    sender = TelegramSender(token=settings.telegram_bot_token)
    fn = _TYPES[args.type]
    today = date.fromisoformat(args.date) if args.date else date.today()

    kwargs = {"sender": sender}
    # plan-evening / plan-morning take `plan_date`, the rest take `today`.
    if args.type in ("plan-evening", "plan-morning"):
        kwargs["plan_date"] = today
    else:
        kwargs["today"] = today

    with session_scope() as session:
        report = fn(session, **kwargs)

    log.info(
        "telegram_digest_done",
        type=args.type,
        **asdict(report),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
