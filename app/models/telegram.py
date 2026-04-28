"""Telegram-channel bookkeeping (FR-CR-04-26)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ProcessedTelegramMessage(Base):
    """One row per Telegram message we've already fed through the
    intent pipeline.

    Telegram source data lives in a read-only Supabase view
    (``humanoid_tg_chats_readonly``) — we never copy the message
    contents into our local DB. We just track which (chat_id,
    message_id) pairs are done so the ingest cron is idempotent and
    can resume.
    """

    __tablename__ = "processed_telegram_messages"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # NULL if the message was processed but didn't yield a task
    # (e.g. classifier returned no_action). We still write the row so
    # we don't reprocess the same message on the next ingest run.
    task_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )


__all__ = ["ProcessedTelegramMessage"]
