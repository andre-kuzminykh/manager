"""Telegram-channel bookkeeping (FR-CR-04-26 / FR-CR-04-27 / FR-CR-05-07)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


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


class TelegramListenerState(Base):
    """FR-CR-04-27 — singleton row holding the last `update_id` we
    acked to Telegram's getUpdates long-polling endpoint.

    Resume-from-this-offset on listener restart so we don't reprocess
    every update Telegram has retained in its 24h queue. The
    `processed_telegram_messages` table still backstops idempotency
    if anything sneaks through (live listener and Supabase ingest
    can race-process the same message).
    """

    __tablename__ = "telegram_listener_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_update_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )


class TelegramChatMember(Base, TimestampMixin):
    """FR-CR-05-07 — per-chat membership we observe on the live
    listener.

    Whenever a message arrives from a Telegram chat we upsert a row
    keyed by ``(chat_id, user_id)`` with whatever profile fields the
    update carries — `username`, `first_name`, `last_name`. The
    classifier pulls the per-chat list as `known_employees` so the
    LLM owner stage can map «Валя сделай X» to the real numeric
    user_id, and the bot can DM the assignee directly when they've
    `/started` the bot at least once (`has_started_bot=True`).
    """

    __tablename__ = "telegram_chat_members"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    has_started_bot: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )


__all__ = [
    "ProcessedTelegramMessage",
    "TelegramListenerState",
    "TelegramChatMember",
]
