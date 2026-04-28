"""Live Telegram message ingestion via the Bot API (FR-CR-04-27).

A long-running worker that polls ``getUpdates`` on the Bot HTTP API,
parses each new message into the same `TelegramSourceMessage` shape
the Supabase view path uses, and feeds it through the existing
`TelegramIngestService`. The data contract is identical: one bookmark
per (chat_id, message_id), `source_kind = 'telegram'` on the resulting
Task, same intent pipeline, same Sheets sync.

Network shape: outbound long-poll only — no public HTTP endpoint, no
inbound port. The bot doesn't have to be reachable from the internet,
just able to reach `api.telegram.org:443`.

To start receiving messages from a group chat, the bot must be:

- added to the chat by an admin;
- have **Group Privacy** turned OFF in BotFather (otherwise it only
  sees `/commands` and direct mentions). Set this in BotFather:
  /mybots → bot → Bot Settings → Group Privacy → Turn off.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.db import session_scope
from app.intent import IntentClassifier
from app.logging_setup import get_logger
from app.models import TelegramListenerState
from app.orchestrator import Orchestrator
from app.telegram_ingest.reader import TelegramSourceMessage
from app.telegram_ingest.service import TelegramIngestService

log = get_logger(__name__)


_API_BASE = "https://api.telegram.org/bot"


@dataclass
class ListenerReport:
    """Counters from one long-poll cycle."""

    updates_seen: int = 0
    messages_processed: int = 0
    tasks_created: int = 0
    no_action: int = 0
    skipped_non_message: int = 0
    errors: int = 0


def parse_update(update: dict[str, Any]) -> TelegramSourceMessage | None:
    """Map a raw `Update` from the Bot API into our internal shape.

    We pick the first message-shaped field present, in priority order:

      1. ``message`` — normal incoming message
      2. ``edited_message`` — user edited a message; we treat it as a
         fresh capture (the bookmark on the original message_id will
         dedupe if it was already processed)
      3. ``channel_post`` — channel post (only seen if bot is admin)
      4. ``edited_channel_post`` — same, edited

    Service messages, callback_queries, my_chat_member events, etc.
    return None — they don't carry a TelegramSourceMessage payload.
    """
    msg = (
        update.get("message")
        or update.get("edited_message")
        or update.get("channel_post")
        or update.get("edited_channel_post")
    )
    if not isinstance(msg, dict):
        return None

    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    message_id = msg.get("message_id")
    if chat_id is None or message_id is None:
        return None

    sender = msg.get("from") or {}
    reply_to = msg.get("reply_to_message") or {}
    sent_at = msg.get("date")
    if isinstance(sent_at, (int, float)):
        sent_at = datetime.fromtimestamp(int(sent_at), tz=timezone.utc)

    text = msg.get("text") or msg.get("caption") or ""

    return TelegramSourceMessage(
        chat_id=int(chat_id),
        message_id=int(message_id),
        text=str(text),
        sent_at=sent_at,
        reply_to=int(reply_to["message_id"]) if reply_to.get("message_id") else None,
        user_id=int(sender["id"]) if sender.get("id") else None,
        user_name=(
            sender.get("username")
            or " ".join(
                p
                for p in (sender.get("first_name"), sender.get("last_name"))
                if p
            ).strip()
            or None
        ),
        chat_title=chat.get("title") or chat.get("username") or None,
        raw=msg,
    )


def _get_offset(session: Session) -> int:
    state = session.get(TelegramListenerState, 1)
    if state is None:
        return 0
    return int(state.last_update_id)


def _save_offset(session: Session, *, last_update_id: int) -> None:
    state = session.get(TelegramListenerState, 1)
    now = datetime.now(timezone.utc)
    if state is None:
        session.add(
            TelegramListenerState(
                id=1, last_update_id=last_update_id, updated_at=now
            )
        )
    else:
        state.last_update_id = last_update_id
        state.updated_at = now
    session.flush()


class TelegramListener:
    """Long-polling worker for the Telegram Bot API.

    The class is split from the entry-point (`ops/telegram_listener.py`)
    so the polling loop is testable: pass a fake `_fetch` and a fake
    `_sleep` and you can drive the worker through fixtures.
    """

    def __init__(
        self,
        *,
        token: str,
        ingest: TelegramIngestService,
        long_poll_timeout: int = 30,
    ) -> None:
        self._token = token
        self._ingest = ingest
        self._long_poll_timeout = long_poll_timeout

    @property
    def enabled(self) -> bool:
        return bool(self._token)

    # ---- API plumbing -----------------------------------------------------

    def _fetch_updates(self, *, offset: int) -> list[dict[str, Any]]:
        """Wrap getUpdates. Returns a list of update dicts (possibly
        empty after the long-poll timeout). Returns ``[]`` on
        transport error and logs a warning — the next iteration will
        retry."""
        if not self._token:
            return []
        import urllib.parse
        import urllib.request

        url = f"{_API_BASE}{self._token}/getUpdates"
        body = urllib.parse.urlencode(
            {
                "timeout": self._long_poll_timeout,
                "offset": offset,
                "allowed_updates": json.dumps(
                    [
                        "message",
                        "edited_message",
                        "channel_post",
                        "edited_channel_post",
                    ]
                ),
            }
        ).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        try:
            with urllib.request.urlopen(
                req, timeout=self._long_poll_timeout + 5
            ) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            log.warning("telegram_get_updates_failed", error=str(e))
            return []
        if not data.get("ok"):
            log.warning(
                "telegram_get_updates_not_ok",
                description=data.get("description"),
            )
            return []
        return list(data.get("result") or [])

    # ---- one tick ---------------------------------------------------------

    def tick(self) -> ListenerReport:
        """Run one long-poll → process → save offset cycle.

        Each tick is its own DB transaction. If anything blows up
        mid-batch the offset isn't persisted, and the next tick
        re-fetches the same range — `processed_telegram_messages`
        backstops dedup on retry.
        """
        with session_scope() as session:
            offset = _get_offset(session)

        updates = self._fetch_updates(offset=offset + 1 if offset else 0)
        if not updates:
            return ListenerReport()

        report = ListenerReport(updates_seen=len(updates))
        max_update_id = offset
        with session_scope() as session:
            for upd in updates:
                update_id = upd.get("update_id")
                if isinstance(update_id, int) and update_id > max_update_id:
                    max_update_id = update_id

                msg = parse_update(upd)
                if msg is None:
                    report.skipped_non_message += 1
                    continue

                try:
                    task = self._ingest.process_one(session, msg)
                    report.messages_processed += 1
                    if task is None:
                        report.no_action += 1
                    else:
                        report.tasks_created += 1
                except Exception as e:  # noqa: BLE001
                    report.errors += 1
                    log.warning(
                        "telegram_listener_process_failed",
                        chat_id=msg.chat_id,
                        message_id=msg.message_id,
                        error=str(e),
                    )

            if max_update_id > offset:
                _save_offset(session, last_update_id=max_update_id)

        return report

    # ---- main loop --------------------------------------------------------

    def run_forever(self, *, sleep_on_idle: float = 1.0) -> None:
        """Block forever, ticking until the process is killed.

        On a long-poll timeout (no updates) Telegram returns an empty
        list immediately — we sleep `sleep_on_idle` seconds before
        the next iteration to avoid hot-spinning if the API misbehaves.
        """
        if not self.enabled:
            log.error(
                "telegram_listener_disabled",
                reason="TELEGRAM_BOT_TOKEN is not set",
            )
            return
        log.info(
            "telegram_listener_starting",
            long_poll_timeout=self._long_poll_timeout,
        )
        while True:
            try:
                report = self.tick()
            except Exception as e:  # noqa: BLE001
                log.warning("telegram_listener_tick_failed", error=str(e))
                time.sleep(sleep_on_idle)
                continue
            if report.updates_seen == 0:
                time.sleep(sleep_on_idle)
            else:
                log.info(
                    "telegram_listener_tick_done",
                    seen=report.updates_seen,
                    processed=report.messages_processed,
                    tasks_created=report.tasks_created,
                    no_action=report.no_action,
                    skipped=report.skipped_non_message,
                    errors=report.errors,
                )
