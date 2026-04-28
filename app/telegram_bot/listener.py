"""Live Telegram message ingestion via the Bot API (FR-CR-04-27).

A long-running worker that polls ``getUpdates`` on the Bot HTTP API,
parses each new message into the same `TelegramSourceMessage` shape
the Supabase view path uses, and feeds it through the existing
`TelegramIngestService`. The data contract is identical: one bookmark
per (chat_id, message_id), `source_kind = 'telegram'` on the resulting
Task, same intent pipeline, same Sheets sync.

FR-CR-04-28 — when a task is created, the listener posts a task card
in the source chat with inline buttons (Start / Mark done / Cancel /
Edit / Delete / Subscribe). Inbound `callback_query` updates from
button presses are dispatched to the Telegram-side handlers in
`app/telegram_bot/handlers.py` and the card is edited in place.

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
from app.models import Task, TelegramListenerState
from app.orchestrator import Orchestrator
from app.telegram_bot import handlers as tg_handlers
from app.telegram_bot.cards import (
    post_initial_card,
    refresh_card,
    render_tombstone,
)
from app.telegram_bot.keyboards import (
    ACTION_CANCEL,
    ACTION_CONFIRM,
    ACTION_DELETE,
    ACTION_DONE,
    ACTION_EDIT,
    ACTION_IGNORE,
    ACTION_START,
    ACTION_SUBSCRIBE,
    ACTION_UNSUBSCRIBE,
    parse_callback_data,
)
from app.telegram_bot.pending import PendingRegistry
from app.telegram_bot.sender import TelegramSender
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
    callbacks_handled: int = 0
    pending_replies_handled: int = 0
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
        sender: TelegramSender | None = None,
        long_poll_timeout: int = 30,
        pending: PendingRegistry | None = None,
    ) -> None:
        self._token = token
        self._ingest = ingest
        # Default sender uses the same token for outbound messages
        # so a single token both reads and writes — that's the
        # standard Bot API setup.
        self._sender = sender if sender is not None else TelegramSender(token=token)
        self._long_poll_timeout = long_poll_timeout
        # FR-CR-04-29 — in-memory state for reply-conversation flows
        # (Mark done with artifact, Edit via key=value reply).
        self._pending = pending if pending is not None else PendingRegistry()

    @property
    def enabled(self) -> bool:
        return bool(self._token)

    def _llm_backend(self):
        """The intent classifier's backend, or None when running in
        rule-only mode. Surfaced so the Edit conversation can reuse the
        same LLM for free-form reply parsing."""
        classifier = getattr(self._ingest, "_classifier", None)
        return getattr(classifier, "backend", None)

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
                        # FR-CR-04-28: inbound button presses
                        "callback_query",
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

                # FR-CR-04-28: inbound button presses come as
                # `callback_query` updates, not messages.
                if "callback_query" in upd:
                    try:
                        self._handle_callback_query(
                            session, upd["callback_query"]
                        )
                        report.callbacks_handled += 1
                    except Exception as e:  # noqa: BLE001
                        report.errors += 1
                        log.warning(
                            "telegram_callback_handler_failed",
                            error=str(e),
                        )
                    continue

                msg = parse_update(upd)
                if msg is None:
                    report.skipped_non_message += 1
                    continue

                # FR-CR-04-29: a reply-to-bot message may be an
                # answer to a previously-posted prompt (artifact
                # for Mark done, key=value payload for Edit). When
                # it is, route to the conversation handler instead
                # of feeding it as a fresh capture.
                pending = self._pending.take(
                    chat_id=msg.chat_id,
                    user_id=msg.user_id or 0,
                    reply_to_message_id=msg.reply_to,
                )
                if pending is not None:
                    try:
                        self._handle_pending_reply(session, pending, msg)
                        report.pending_replies_handled += 1
                    except tg_handlers.NotAuthorised as e:
                        self._sender.send_message(
                            chat_id=msg.chat_id,
                            text=f":lock: {e}",
                            reply_to_message_id=msg.message_id,
                        )
                    except Exception as e:  # noqa: BLE001
                        log.warning(
                            "telegram_pending_reply_failed",
                            action=pending.action,
                            task_id=pending.task_id,
                            error=str(e),
                        )
                        report.errors += 1
                    continue

                try:
                    task = self._ingest.process_one(session, msg)
                    report.messages_processed += 1
                    if task is None:
                        report.no_action += 1
                    else:
                        report.tasks_created += 1
                        # Post the live card under the source message
                        # so the user can drive the lifecycle without
                        # leaving Telegram.
                        try:
                            post_initial_card(
                                sender=self._sender,
                                session=session,
                                task=task,
                                chat_id=msg.chat_id,
                                reply_to_message_id=msg.message_id,
                                author_user_id=str(msg.user_id) if msg.user_id else None,
                            )
                        except Exception as e:  # noqa: BLE001
                            log.warning(
                                "telegram_post_initial_card_failed",
                                task_id=task.id,
                                error=str(e),
                            )
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

    # ---- callback_query routing ------------------------------------------

    def _handle_callback_query(
        self, session: Session, cq: dict[str, Any]
    ) -> None:
        """Dispatch a single button press to the matching handler.

        Always answers the callback_query (Telegram requires this
        within ~15s) — with a notification message when the action
        was rejected (e.g. "Only the owner can delete this") so the
        user gets immediate feedback.
        """
        cq_id = cq.get("id")
        actor_obj = cq.get("from") or {}
        actor = str(actor_obj.get("id")) if actor_obj.get("id") is not None else None
        data = cq.get("data") or ""
        parsed = parse_callback_data(data)
        if parsed is None:
            self._sender.answer_callback_query(callback_query_id=cq_id, text="?")
            return
        action, entity_id = parsed

        try:
            task = self._dispatch_action(
                session, action=action, entity_id=entity_id, actor=actor, cq=cq
            )
        except tg_handlers.NotAuthorised as e:
            self._sender.answer_callback_query(
                callback_query_id=cq_id, text=str(e)
            )
            return
        except Exception as e:  # noqa: BLE001
            log.warning(
                "telegram_callback_dispatch_failed",
                action=action,
                entity_id=entity_id,
                error=str(e),
            )
            self._sender.answer_callback_query(
                callback_query_id=cq_id, text="Error, try again."
            )
            return

        self._sender.answer_callback_query(callback_query_id=cq_id)
        if task is None:
            return

        # Re-render the card to reflect the new state (status,
        # buttons, subscription label, etc.)
        if action == ACTION_DELETE:
            render_tombstone(sender=self._sender, task=task, actor=actor)
        else:
            refresh_card(
                sender=self._sender,
                session=session,
                task=task,
                viewer=actor,
            )

    def _dispatch_action(
        self,
        session: Session,
        *,
        action: str,
        entity_id: int,
        actor: str | None,
        cq: dict[str, Any],
    ) -> Task | None:
        if not actor:
            raise tg_handlers.NotAuthorised("Couldn't identify your user.")
        if action == ACTION_START:
            return tg_handlers.handle_start(session, task_id=entity_id, actor=actor)
        if action == ACTION_DONE:
            # FR-CR-04-29: open the optional-artifact reply
            # conversation instead of transitioning immediately.
            return self._open_done_conversation(
                session, task_id=entity_id, actor=actor, cq=cq
            )
        if action == ACTION_CANCEL:
            return tg_handlers.handle_cancel(session, task_id=entity_id, actor=actor)
        if action == ACTION_DELETE:
            return tg_handlers.handle_delete(session, task_id=entity_id, actor=actor)
        if action == ACTION_SUBSCRIBE:
            return tg_handlers.handle_subscribe(
                session, task_id=entity_id, actor=actor, subscribe=True
            )
        if action == ACTION_UNSUBSCRIBE:
            return tg_handlers.handle_subscribe(
                session, task_id=entity_id, actor=actor, subscribe=False
            )
        if action == ACTION_EDIT:
            # FR-CR-04-29: open the key=value reply conversation.
            return self._open_edit_conversation(
                session, task_id=entity_id, actor=actor, cq=cq
            )
        if action in (ACTION_CONFIRM, ACTION_IGNORE):
            # Draft cards aren't posted yet (we create tasks
            # immediately on ingest). These actions are reserved for
            # the next iteration when a confirm-first flow lands.
            return None
        log.info("telegram_unknown_action", action=action)
        return None

    # ---- conversation flows (Mark done with artifact, Edit) -------------

    def _open_done_conversation(
        self,
        session: Session,
        *,
        task_id: int,
        actor: str,
        cq: dict[str, Any],
    ) -> Task | None:
        """Click on Mark done → post the artifact prompt and register
        a pending question. The user's reply (text or `/skip`) lands
        in `_handle_pending_reply` on the next tick."""
        task, prompt_text = tg_handlers.prompt_done(
            session, task_id=task_id, actor=actor
        )
        chat = (cq.get("message") or {}).get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return None
        resp = self._sender.send_message(
            chat_id=chat_id,
            text=prompt_text,
            reply_markup={"force_reply": True, "selective": True},
        )
        prompt_msg_id = resp.get("message_id")
        if prompt_msg_id:
            self._pending.register(
                action="artifact",
                task_id=task_id,
                chat_id=int(chat_id),
                user_id=int(actor),
                prompt_message_id=int(prompt_msg_id),
            )
        # Returning None means the listener won't try to refresh
        # the original card right now — it'll happen after the
        # user replies.
        return None

    def _open_edit_conversation(
        self,
        session: Session,
        *,
        task_id: int,
        actor: str,
        cq: dict[str, Any],
    ) -> Task | None:
        task, prompt_text = tg_handlers.prompt_edit(
            session, task_id=task_id, actor=actor
        )
        chat = (cq.get("message") or {}).get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return None
        resp = self._sender.send_message(
            chat_id=chat_id,
            text=prompt_text,
            reply_markup={"force_reply": True, "selective": True},
        )
        prompt_msg_id = resp.get("message_id")
        if prompt_msg_id:
            self._pending.register(
                action="edit",
                task_id=task_id,
                chat_id=int(chat_id),
                user_id=int(actor),
                prompt_message_id=int(prompt_msg_id),
            )
        return None

    def _handle_pending_reply(
        self,
        session: Session,
        pending,  # PendingQuestion
        msg: TelegramSourceMessage,
    ) -> None:
        """Apply the user's reply to a prompt. ``pending.action``
        decides whether to complete the task with an artifact or
        apply an edit payload."""
        actor = str(msg.user_id) if msg.user_id else None
        if not actor:
            return

        if pending.action == "artifact":
            task = tg_handlers.apply_done_artifact_reply(
                session,
                task_id=pending.task_id,
                actor=actor,
                reply_text=msg.text,
            )
            if task is not None:
                refresh_card(
                    sender=self._sender,
                    session=session,
                    task=task,
                    viewer=actor,
                )
        elif pending.action == "edit":
            # Pass the same LLM backend that drives intent extraction
            # so the user can write the Edit reply in free-form natural
            # language ("сдвинь срок на пятницу, приоритет высокий").
            backend = self._llm_backend()
            task = tg_handlers.apply_edit_reply(
                session,
                task_id=pending.task_id,
                actor=actor,
                reply_text=msg.text,
                llm_backend=backend,
            )
            if task is not None:
                refresh_card(
                    sender=self._sender,
                    session=session,
                    task=task,
                    viewer=actor,
                )
        else:
            log.info("telegram_unknown_pending_action", action=pending.action)

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
