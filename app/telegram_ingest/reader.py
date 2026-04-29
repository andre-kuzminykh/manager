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


def _has_only_ipv6(database_url: str) -> bool:
    """True when the URL's host has at least one AAAA but no A records.

    Used to print a friendly "switch to the pooler URL" hint instead
    of letting libpq blow up on an unreachable IPv6 address.
    """
    import socket
    from urllib.parse import urlparse

    try:
        parsed = urlparse(database_url)
        host = parsed.hostname
        port = parsed.port or 5432
    except Exception:  # noqa: BLE001
        return False
    if not host:
        return False
    try:
        v4 = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        v4 = []
    try:
        v6 = socket.getaddrinfo(host, port, socket.AF_INET6, socket.SOCK_STREAM)
    except OSError:
        v6 = []
    return not v4 and bool(v6)


def _resolve_ipv4(database_url: str) -> str | None:
    """Best-effort IPv4 lookup for the host in a SQLAlchemy DSN.

    Returns the dotted-quad string if the host has at least one A
    record, otherwise None (we then fall through to libpq's default
    resolution, which may pick an AAAA record).

    Used to dodge the "Supabase free tier serves IPv6 only" gotcha
    on cloud VMs without outbound IPv6.
    """
    import socket
    from urllib.parse import urlparse

    try:
        parsed = urlparse(database_url)
        host = parsed.hostname
        port = parsed.port or 5432
    except Exception:  # noqa: BLE001
        return None
    if not host:
        return None
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return None
    if not infos:
        return None
    return infos[0][4][0]


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
    # Telegram chat type: "private" (1:1 DM with the bot), "group",
    # "supergroup", "channel". Used to decide whether a task-shaped
    # message goes through the immediate-create or confirm-first
    # flow (FR-CR-04-32). When unknown (e.g. Supabase ingest), we
    # default to "supergroup" for negative chat_ids and "private" for
    # positive ones — the same convention Telegram uses.
    chat_type: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_textual(self) -> bool:
        """True when the message has any usable text content."""
        return bool((self.text or "").strip())

    @property
    def is_private(self) -> bool:
        """True for 1:1 DMs with the bot. Falls back to chat_id sign
        when `chat_type` wasn't provided (the convention is positive
        ids = private, negative ids = group/supergroup/channel)."""
        if self.chat_type:
            return self.chat_type == "private"
        return self.chat_id > 0


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

            connect_args: dict[str, str] = {
                # Read-only role; setting `default_transaction_read_only`
                # via connect_args defends against accidental writes.
                "options": "-c default_transaction_read_only=on",
            }
            # Supabase quirk: `db.<project>.supabase.co` resolves to an
            # IPv6 address on the free tier, and many cloud VMs (incl.
            # GCE in europe-west1 with the default network) don't have
            # outbound IPv6. Pre-resolve the hostname to IPv4 ourselves
            # and pass it to libpq via `hostaddr` so it never tries the
            # AAAA record. The `host` field stays the original hostname
            # so TLS SNI / cert verification still works.
            ipv4 = _resolve_ipv4(url)
            if ipv4 is not None:
                connect_args["hostaddr"] = ipv4
            elif _has_only_ipv6(url):
                # Fail fast with a clear message instead of letting
                # libpq spam an unreachable IPv6 address.
                from urllib.parse import urlparse

                host = urlparse(url).hostname
                log.warning(
                    "telegram_source_host_is_ipv6_only",
                    host=host,
                    hint=(
                        "This Supabase host has no IPv4 A record and "
                        "the VM has no outbound IPv6. Switch "
                        "TELEGRAM_SOURCE_DATABASE_URL to the Supabase "
                        "Pooler URL "
                        "(aws-0-<region>.pooler.supabase.com)."
                    ),
                )

            self._engine = create_engine(
                url,
                pool_pre_ping=True,
                connect_args=connect_args,
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

    def _detect_columns(self) -> set[str]:
        """Return the set of column names present in the configured
        view. Cached on the instance after the first call.

        We probe the view via a zero-row LIMIT 0 query; the result's
        `keys()` carry the column names. Cheaper than asking
        `information_schema` and works against any backend.
        """
        cached = getattr(self, "_columns_cache", None)
        if cached is not None:
            return cached
        sql = text(f"SELECT * FROM {self._view} LIMIT 0")
        with self._engine.connect() as conn:
            result = conn.execute(sql)
            cols = set(result.keys())
        self._columns_cache = cols
        return cols

    def recent_in_chat(
        self,
        *,
        chat_id: int,
        before_message_id: int,
        max_chars: int = 10_000,
        step: int = 10,
        max_messages: int = 50,
    ) -> list[TelegramSourceMessage]:
        """FR-CR-05-09 — fetch the **adaptive context window** for a
        single chat: prior messages older than ``before_message_id``,
        ordered newest-first by ``(sent_at, message_id)``, expanded
        in increments of ``step`` until the combined ``text`` length
        crosses ``max_chars`` or we've pulled ``max_messages``.

        Returns the messages **chronologically (oldest-first)** so
        the caller can hand them to the intent pipeline as
        ``ContextWindow.history_before`` directly.
        """
        if self._engine is None:
            return []
        if before_message_id is None:
            return []
        present = self._detect_columns()
        date_cols = [c for c in _FIELD_MAP["sent_at"] if c in present]
        if date_cols:
            coalesce_expr = "COALESCE(" + ", ".join(date_cols) + ")"
            order_clause = (
                f"ORDER BY {coalesce_expr} DESC NULLS LAST, "
                "message_id DESC"
            )
        else:
            order_clause = "ORDER BY message_id DESC"

        sql = text(
            f"""
            SELECT * FROM {self._view}
            WHERE chat_id = :chat_id
              AND message_id < :msg_id
            {order_clause}
            LIMIT :lim
            """
        )

        # Pull progressively larger pages — 10, 20, 30 … up to
        # ``max_messages`` — and stop as soon as the accumulated text
        # crosses ``max_chars``. Doing it in one query with a generous
        # LIMIT and trimming in Python is simpler and equally fast for
        # these sizes.
        accumulated: list[TelegramSourceMessage] = []
        with self._engine.connect() as conn:
            limit = min(max_messages, max(step, 10))
            while True:
                accumulated = []
                total = 0
                result = conn.execute(
                    sql,
                    {
                        "chat_id": int(chat_id),
                        "msg_id": int(before_message_id),
                        "lim": limit,
                    },
                )
                rows = list(result.mappings())
                for row in rows:
                    msg = _map_row(dict(row))
                    if msg is None:
                        continue
                    accumulated.append(msg)
                    total += len(msg.text or "")
                    if total >= max_chars:
                        break
                if total >= max_chars or len(rows) < limit:
                    # Either we have enough chars, or the chat doesn't
                    # have any more messages — stop expanding.
                    break
                if limit >= max_messages:
                    break
                limit = min(limit + step, max_messages)

        # Reverse — caller wants oldest first (chronological order).
        return list(reversed(accumulated))

    def iter_newest(
        self, *, limit: int
    ) -> Iterable[TelegramSourceMessage]:
        """Yield the ``limit`` most recently-sent messages (newest-
        first by ``sent_at``). Used by ``migrate_telegram_history
        --newest`` to grab «latest 50 messages, regardless of which
        chat they came from».

        The view's date column varies (`date` / `sent_at` /
        `created_at` / `timestamp`). We probe the schema upfront and
        build a `COALESCE(...)` over ONLY the columns that actually
        exist — feeding a non-existent column to PostgreSQL would
        raise `UndefinedColumn` AND abort the transaction (so even a
        fallback query fails). When the view has no date column at
        all, fall back to ordering by `(chat_id DESC, message_id
        DESC)` — close enough for «most recent within each chat».
        """
        if self._engine is None:
            return iter(())

        present = self._detect_columns()
        date_cols = [c for c in _FIELD_MAP["sent_at"] if c in present]
        if date_cols:
            coalesce_expr = "COALESCE(" + ", ".join(date_cols) + ")"
            order_clause = (
                f"ORDER BY {coalesce_expr} DESC NULLS LAST, "
                "chat_id DESC, message_id DESC"
            )
        else:
            log.warning(
                "telegram_iter_newest_no_date_column",
                view=self._view,
                tried=list(_FIELD_MAP["sent_at"]),
                present=sorted(present),
            )
            order_clause = "ORDER BY chat_id DESC, message_id DESC"

        sql = text(
            f"""
            SELECT * FROM {self._view}
            {order_clause}
            LIMIT :lim
            """
        )
        with self._engine.connect() as conn:
            result = conn.execute(sql, {"lim": limit})
            for row in result.mappings():
                msg = _map_row(dict(row))
                if msg is not None:
                    yield msg
