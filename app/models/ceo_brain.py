"""FR-CB2-200 — CEO Brain Bot data models.

Two tables:
  * ``slack_message_archive`` — append-only archive of every Slack
    message in channels where the bot is a member. UNIQUE
    constraint on (channel_id, ts) so retries / replays are
    idempotent.
  * ``claude_responder_runs`` — per @mention / DM-reply run record:
    request payload (scrubbed), final response, tool_uses, cost,
    status. One row per attempt.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class SlackMessageArchive(Base, TimestampMixin):
    __tablename__ = "slack_message_archive"
    __table_args__ = (
        UniqueConstraint(
            "channel_id", "ts", name="uq_slack_message_archive_channel_ts"
        ),
        Index("ix_slack_message_archive_day", "day"),
        Index(
            "ix_slack_message_archive_channel_day", "channel_id", "day"
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True,
    )
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    channel_name: Mapped[str | None] = mapped_column(
        String(255), nullable=True,
    )
    ts: Mapped[str] = mapped_column(String(64), nullable=False)
    thread_ts: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    user_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    user_display_name: Mapped[str | None] = mapped_column(
        String(255), nullable=True,
    )
    subtype: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True,
    )
    edit_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    day: Mapped[Any] = mapped_column(Date, nullable=False)


class ClaudeResponderRun(Base, TimestampMixin):
    __tablename__ = "claude_responder_runs"
    __table_args__ = (
        Index(
            "ix_claude_responder_runs_channel_ts",
            "slack_channel_id",
            "slack_event_ts",
        ),
        Index("ix_claude_responder_runs_status", "status"),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True,
    )
    slack_channel_id: Mapped[str] = mapped_column(
        String(64), nullable=False,
    )
    slack_event_ts: Mapped[str] = mapped_column(
        String(64), nullable=False,
    )
    slack_placeholder_ts: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    request_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True,
    )
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_uses: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON, nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending",
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_usd: Mapped[Any] = mapped_column(
        Numeric(10, 4), nullable=False, default=0,
    )
    input_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
    )
    output_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
    )
    cache_read_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
    )
    cache_write_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )


__all__ = ["ClaudeResponderRun", "SlackMessageArchive"]
