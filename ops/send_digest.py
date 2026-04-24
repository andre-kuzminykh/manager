"""Send daily / weekly / deadlines digest (FR-CR-6).

Usage:
    python -m ops.send_digest --type daily
    python -m ops.send_digest --type weekly
    python -m ops.send_digest --type deadlines

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
from app.services import DigestKind, DigestService, send_weekly_plan
from app.slack_bot.rate_limiter import RateAwareSlackSender

log = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--type",
        required=True,
        choices=[k.value for k in DigestKind] + ["weekly-plan"],
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
