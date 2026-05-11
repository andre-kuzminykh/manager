"""FR-CR-05-162 — Slack Socket-Mode ingest. Listens to channels
where the bot is added, extracts tasks via LLM, posts cards in
Telegram (no Slack-side output). Feature-flagged via
SLACK_INGEST_ENABLED."""

from app.slack_ingest.listener import (
    make_slack_ingest_app,
    run_socket_mode,
)

__all__ = ["make_slack_ingest_app", "run_socket_mode"]
