"""Telegram-channel bot (FR-CR-04-26).

Minimal skeleton mirroring the Slack bot's outbound surface so the
ingest worker can post draft / task cards back into Telegram chats.

This package intentionally stays small for now — the sender + a
couple of inline-keyboard helpers are enough for the MVP. The full
button-driven UX (Confirm / Edit / Reject / Start / Mark done /
Cancel / Delete / Subscribe) is the next iteration; see SPEC.md
section FR-CR-04-26 for the user-facing roadmap.
"""
from app.telegram_bot.sender import TelegramSender, build_task_card_text
from app.telegram_bot.keyboards import (
    confirm_keyboard,
    task_card_keyboard,
)

__all__ = [
    "TelegramSender",
    "build_task_card_text",
    "confirm_keyboard",
    "task_card_keyboard",
]
