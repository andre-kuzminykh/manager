from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class SlackConversation(Base, TimestampMixin):
    __tablename__ = "slack_conversations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # channel id
    team_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # im / mpim / channel / group
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    messages: Mapped[list["SlackMessage"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )


class SlackMessage(Base, TimestampMixin):
    __tablename__ = "slack_messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "ts", name="uq_slack_messages_conv_ts"),
        Index("ix_slack_messages_thread", "thread_ts"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("slack_conversations.id"), nullable=False
    )
    ts: Mapped[str] = mapped_column(String(32), nullable=False)
    thread_ts: Mapped[str | None] = mapped_column(String(32), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subtype: Mapped[str | None] = mapped_column(String(64), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    has_audio: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    permalink: Mapped[str | None] = mapped_column(String(512), nullable=True)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    conversation: Mapped[SlackConversation] = relationship(back_populates="messages")


class SlackEventArchive(Base):
    """Unfiltered audit log: one row per Slack event the bot received,
    BEFORE any ignore-filtering. Use this for forensic / analytics work.

    Unlike `slack_messages`, this table:
    - has no unique constraint (`message_changed`, `message_deleted`,
      and replays each get their own row);
    - keeps the full Slack event JSON in `raw`;
    - records non-message events too (app_mention, file_share, …);
    - includes the audio transcript when one was produced.
    """

    __tablename__ = "slack_events_archive"
    __table_args__ = (
        Index("ix_slack_events_archive_event_id", "event_id"),
        Index("ix_slack_events_archive_conv_ts", "conversation_id", "ts"),
        Index("ix_slack_events_archive_received_at", "received_at"),
    )

    # BigInt on Postgres so we can keep millions of rows; Integer on
    # SQLite (test backend) because SQLite's BigInteger PK doesn't
    # auto-increment.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subtype: Mapped[str | None] = mapped_column(String(64), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ts: Mapped[str | None] = mapped_column(String(32), nullable=True)
    thread_ts: Mapped[str | None] = mapped_column(String(32), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ContextSnapshot(Base, TimestampMixin):
    """Serialized context window used for intent classification and persisted for audit."""

    __tablename__ = "context_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ts: Mapped[str] = mapped_column(String(32), nullable=False)
    thread_ts: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_message: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    history_before: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    thread_messages: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )


class ProcessedSlackEvent(Base):
    """Dedup store: Slack retries events, we must process each unique event_id only once."""

    __tablename__ = "processed_slack_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
