"""One-shot CLI to backfill the Employees directory from Slack.

Usage:
    python -m ops.sync_employees

Pulls every workspace member via `users.list` and upserts them into
the local employees table. Run this once after a fresh deploy so the
owner LLM has the full team to pick from before the bot has seen
anyone post.
"""
from __future__ import annotations

import sys

from slack_sdk import WebClient

from app.config import get_settings
from app.db import session_scope
from app.logging_setup import get_logger, setup_logging
from app.services import EmployeeDirectory

log = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    settings = get_settings()
    if not settings.slack_bot_token:
        log.error("missing_slack_bot_token")
        return 2

    client = WebClient(token=settings.slack_bot_token)
    directory = EmployeeDirectory(client=client, settings=settings)
    with session_scope() as session:
        touched = directory.sync_workspace_members(session)
    log.info("employees_sync_done", touched=touched)
    return 0


if __name__ == "__main__":
    sys.exit(main())
