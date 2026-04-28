"""Telegram-channel ingestion (FR-CR-04-26).

Reads new messages from the read-only Supabase view
``humanoid_tg_chats_readonly``, runs them through the existing
intent pipeline, persists ActionDraft + Task rows in our local DB,
and (optionally) posts a draft card to Telegram via the bot.

This package is decoupled from `app.slack_bot`: the same models,
intent pipeline, persistence layer, and Sheets sync are reused, so a
Telegram task lands in the exact same task table as a Slack one and
shows up in the same Google Sheet.
"""
from app.telegram_ingest.reader import (
    TelegramSourceMessage,
    TelegramSourceReader,
)
from app.telegram_ingest.service import TelegramIngestService

__all__ = [
    "TelegramSourceMessage",
    "TelegramSourceReader",
    "TelegramIngestService",
]
