"""Read-only client for the Supabase Telegram message view.

The team's Telegram messages are collected by a separate ingestion
pipeline (out of scope for us) and exposed as a read-only Postgres
view ``humanoid_tg_chats_readonly``. We mount it via SQLAlchemy and
read pages of new messages. We never write to it.

The exact schema of the view is deliberately not hard-coded here —
it varies between deployments. We probe the view's columns at first
read and map a small set of well-known names to our internal
``TelegramSourceMessage`` dataclass:

  - chat_id        ← `chat_id` / `chatid`
  - message_id     ← `message_id` / `messageid` / `id`
  - reply_to       ← `reply_to_message_id` / `reply_to`
  - user_id        ← `from_user_id` / `user_id` / `sender_id`
  - user_name      ← `from_user_name` / `user_name`
  - chat_title     ← `chat_title` / `title`
  - text           ← `text` / `message_text` / `body`
  - sent_at        ← `date` / `sent_at` / `created_at`

Anything we don't recognise is dropped on the floor (the
``raw`` field carries the unmapped row for debugging).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.logging_setup import get_logger

log = get_logger(__name__)


_FIELD_MAP: dict[str, tuple[str, ...]] = {
    "chat_id": ("chat_id", "chatid"),
    "message_id": ("message_id", "messageid", "id"),
    "reply_to": ("reply_to_message_id", "reply_to"),
    "user_id": ("from_user_id", "user_id", "sender_id"),
    "user_name": (
        "from_user_name",
        "user_name",
        "username",
        "from_user",
        "sender_name",
    ),
    "chat_title": ("chat_title", "title", "chat_name"),
    "text": ("text", "message_text", "body", "content"),
    "sent_at": ("date", "sent_at", "created_at", "timestamp"),
}


@dataclass
class TelegramSourceMessage:
    """A single Telegram message after being mapped from the
    Supabase view's row shape."""

    chat_id: int
    message_id: int
    text: str
    sent_at: datetime | None = None
    reply_to: int | None = None
    user_id: int | None = None
    user_name: str | None = None
    chat_title: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_textual(self) -> bool:
        """True when the message has any usable text content."""
        return bool((self.text or "").strip())


def _pick(row: dict[str, Any], keys: tuple[str, ...]) -> Any | None:
    for k in keys:
        if k in row and row[k] is not None:
            return row[k]
    return None


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _map_row(row: dict[str, Any]) -> TelegramSourceMessage | None:
    chat = _coerce_int(_pick(row, _FIELD_MAP["chat_id"]))
    msg = _coerce_int(_pick(row, _FIELD_MAP["message_id"]))
    if chat is None or msg is None:
        # Rows without identifiers are useless to us — skip silently.
        return None
    return TelegramSourceMessage(
        chat_id=chat,
        message_id=msg,
        text=str(_pick(row, _FIELD_MAP["text"]) or ""),
        sent_at=_pick(row, _FIELD_MAP["sent_at"]),
        reply_to=_coerce_int(_pick(row, _FIELD_MAP["reply_to"])),
        user_id=_coerce_int(_pick(row, _FIELD_MAP["user_id"])),
        user_name=(_pick(row, _FIELD_MAP["user_name"]) or None),
        chat_title=(_pick(row, _FIELD_MAP["chat_title"]) or None),
        raw=dict(row),
    )


class TelegramSourceReader:
    """Pages through the Supabase Telegram view in (chat_id,
    message_id) order, optionally limited by a starting watermark.

    Designed to be paired with `processed_telegram_messages` for
    idempotent resume — the ingest worker queries us for "everything
    after the last seen pair", processes each row, and records it.
    """

    def __init__(
        self,
        *,
        database_url: str,
        view_name: str = "humanoid_tg_chats_readonly",
        engine: Engine | None = None,
    ) -> None:
        if engine is not None:
            self._engine = engine
        elif database_url:
            # SQLAlchemy defaults `postgresql://` to the legacy
            # psycopg2 driver — we ship psycopg3 only, so normalise
            # the scheme to `postgresql+psycopg://` (no-op if the
            # caller already did it).
            url = database_url
            if url.startswith("postgresql://"):
                url = "postgresql+psycopg://" + url[len("postgresql://"):]
            elif url.startswith("postgres://"):
                url = "postgresql+psycopg://" + url[len("postgres://"):]
            self._engine = create_engine(
                url,
                pool_pre_ping=True,
                # Read-only role; setting `default_transaction_read_only`
                # via connect_args defends against accidental writes.
                connect_args={"options": "-c default_transaction_read_only=on"},
            )
        else:
            self._engine = None
        self._view = view_name

    @property
    def configured(self) -> bool:
        return self._engine is not None

    def page(
        self,
        *,
        after_chat_id: int | None = None,
        after_message_id: int | None = None,
        limit: int = 200,
    ) -> Iterator[TelegramSourceMessage]:
        """Yield up to `limit` messages strictly after the given
        watermark. The first call typically passes ``after_*=None``;
        subsequent calls pass the last yielded values to resume."""
        if self._engine is None:
            return iter(())

        if after_chat_id is None or after_message_id is None:
            sql = text(
                f"""
                SELECT * FROM {self._view}
                ORDER BY chat_id ASC, message_id ASC
                LIMIT :lim
                """
            )
            params = {"lim": limit}
        else:
            sql = text(
                f"""
                SELECT * FROM {self._view}
                WHERE (chat_id, message_id) > (:c, :m)
                ORDER BY chat_id ASC, message_id ASC
                LIMIT :lim
                """
            )
            params = {"c": after_chat_id, "m": after_message_id, "lim": limit}

        with self._engine.connect() as conn:
            result = conn.execute(sql, params)
            for row in result.mappings():
                msg = _map_row(dict(row))
                if msg is not None:
                    yield msg

    def iter_all(
        self, *, batch_size: int = 200
    ) -> Iterable[TelegramSourceMessage]:
        """Stream every message in the view in stable order. Used by
        the one-shot history migration (``ops/migrate_telegram_history.py``).
        """
        last_chat: int | None = None
        last_msg: int | None = None
        while True:
            yielded = 0
            for m in self.page(
                after_chat_id=last_chat,
                after_message_id=last_msg,
                limit=batch_size,
            ):
                last_chat, last_msg = m.chat_id, m.message_id
                yield m
                yielded += 1
            if yielded < batch_size:
                # Shorter-than-asked-for page → end of view.
                break
