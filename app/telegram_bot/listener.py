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
from typing import Any, Callable, Iterable

from sqlalchemy.orm import Session

from app.db import session_scope
from app.intent import IntentClassifier
from app.logging_setup import get_logger
from app.models import Task, TelegramListenerState
from app.orchestrator import Orchestrator
from app.telegram_bot import handlers as tg_handlers
from app.telegram_bot.cards import (
    post_draft_confirmation,
    post_initial_card,
    refresh_card,
    refresh_draft_widgets,
    render_draft_rejected,
    render_tombstone,
    replace_widgets_with_task_card,
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
from app.telegram_bot.sender import TelegramSender, build_task_card_text
from app.telegram_ingest.reader import TelegramSourceMessage
from app.telegram_ingest.service import TelegramIngestService

log = get_logger(__name__)


# FR-CR-05-45 — welcome widget shown on `/start` (and `/help`) in
# any private DM with the bot. Read by `tick()` before any ingest
# routing. Single source of truth so the message stays consistent
# across re-deploys.
_WELCOME_WIDGET_TEXT = (
    "👋 <b>Hi! I keep your task list.</b>\n\n"
    "📝 Send a task as text or dictate it as voice — I'll parse "
    "it. You can also list several at once: <i>«first task …, "
    "second task …»</i> — and I'll split them into separate "
    "cards.\n\n"
    "🚦 Each card has buttons: <b>Start</b>, <b>Edit</b>, "
    "<b>Mark done</b>, <b>Subscribe</b>. Tap Edit and reply "
    "right under the prompt — text or voice both work.\n\n"
    "📊 Every evening I'll send a short status of all tasks.\n"
    "☀ Every morning — the cards for today, ordered by priority.\n\n"
    "Let's start — what needs to be done?"
)


_API_BASE = "https://api.telegram.org/bot"


@dataclass
class ListenerReport:
    """Counters from one long-poll cycle."""

    updates_seen: int = 0
    messages_processed: int = 0
    tasks_created: int = 0
    drafts_proposed: int = 0
    no_action: int = 0
    skipped_non_message: int = 0
    skipped_pre_startup: int = 0  # FR-CR-05-51
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
            # When the sender has a `username`, store it with the
            # leading `@` so cards / Sheet show the canonical Telegram
            # handle (e.g. `@andre_andreevich`). Falls back to the
            # display name otherwise.
            (f"@{sender['username']}" if sender.get("username") else None)
            or " ".join(
                p
                for p in (sender.get("first_name"), sender.get("last_name"))
                if p
            ).strip()
            or None
        ),
        chat_title=chat.get("title") or chat.get("username") or None,
        chat_type=chat.get("type") or None,
        raw=msg,
    )


def _upsert_member_from_update(session: Session, update: dict[str, Any]) -> None:
    """Pull `(chat_id, from)` out of any message-shaped update and
    upsert the row in `telegram_chat_members`. Skipped silently if
    the update doesn't carry a sender (service updates,
    callback_query, etc.)."""
    msg = (
        update.get("message")
        or update.get("edited_message")
        or update.get("channel_post")
        or update.get("edited_channel_post")
    )
    if not isinstance(msg, dict):
        return
    chat = msg.get("chat") or {}
    sender = msg.get("from") or {}
    chat_id = chat.get("id")
    user_id = sender.get("id")
    if chat_id is None or user_id is None:
        return
    from app.services.telegram_members import upsert_member

    upsert_member(
        session,
        chat_id=int(chat_id),
        user_id=int(user_id),
        username=sender.get("username") or None,
        first_name=sender.get("first_name") or None,
        last_name=sender.get("last_name") or None,
        # Private-chat traffic proves the user /started the bot —
        # mark them as DM-able for downstream consumers.
        has_started_bot=(chat.get("type") == "private"),
    )



_AT_MENTION_RE = __import__("re").compile(r"@[A-Za-z][A-Za-z0-9_]{4,31}\b")


def _has_at_mention(text: str | None) -> bool:
    """Detect a Telegram username mention (``@andre_andreevich``).

    The regex matches the same character class Telegram uses for
    usernames (5-32 chars, ASCII alnum + underscore, must start with
    a letter). When a contributor types ``@petya подготовь презу``
    the intent is unambiguous, so the listener can skip the
    confirm-first widget and create the task immediately.

    A false positive would be an email-like ``user@example.com`` —
    accepted, but the «task» it produces is harmless and easy to
    Reject via the regular task card.
    """
    if not text:
        return False
    return bool(_AT_MENTION_RE.search(text))


def _looks_like_confirm_widget(cq: dict[str, Any]) -> bool:
    """A draft confirm widget always carries exactly the row
    ``[confirm, edit, ignore]``. Detect that pattern on the clicked
    message so we can route Edit to a friendly stub instead of the
    task-edit flow (FR-CR-04-32)."""
    msg = cq.get("message") or {}
    rm = msg.get("reply_markup") or {}
    rows = rm.get("inline_keyboard") or []
    if len(rows) != 1 or len(rows[0]) != 3:
        return False
    actions = []
    for b in rows[0]:
        cd = (b or {}).get("callback_data") or ""
        actions.append(cd.split(":", 1)[0])
    # FR-CR-05-34 — current layout is [ignore, edit, confirm].
    # Pre-FR-CR-05-34 widgets in the wild may still carry the
    # legacy [confirm, edit, ignore] order; accept either.
    return sorted(actions) == sorted(
        [ACTION_CONFIRM, ACTION_EDIT, ACTION_IGNORE]
    ) and len(actions) == 3


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
        team_sheet_factory=None,
        tasks_sheet_pull_factory=None,
        sheet_poll_interval_seconds: int = 60,
        view_realtime_enabled: bool = False,
        view_poll_interval_seconds: int = 30,
        view_poll_batch_size: int = 50,
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
        # FR-CR-05-28 — periodic Sheet → DB polling. The listener's
        # main loop is a long-poll on getUpdates that wakes up every
        # ≤30s; on each wake we also tick the Sheet pulls when the
        # poll interval has elapsed. No external cron needed.
        self._team_sheet_factory = team_sheet_factory
        self._tasks_sheet_pull_factory = tasks_sheet_pull_factory
        self._sheet_poll_interval = max(0, int(sheet_poll_interval_seconds))
        self._last_sheet_poll_at = 0.0
        # FR-CR-05-35 — periodic poll of the Supabase TG message
        # view. Off by default — flip via VIEW_REALTIME_ENABLED.
        self._view_realtime_enabled = bool(view_realtime_enabled)
        self._view_poll_interval = max(0, int(view_poll_interval_seconds))
        self._view_poll_batch = max(1, int(view_poll_batch_size))
        self._last_view_poll_at = 0.0
        # FR-CR-05-51 — «получай данные с сейчас, в старое не
        # ходи». Set on the first poll; messages with `sent_at`
        # strictly before this timestamp are skipped so the
        # listener never backfills history on startup. Old
        # captures are still available via
        # `ops.migrate_telegram_history` when actually needed.
        self._view_realtime_started_at: datetime | None = None
        # Same cutoff for the Bot API getUpdates path — Telegram
        # holds up to 24h of undelivered updates after a cold
        # start, and we don't want those ancient messages
        # spawning fresh task cards either.
        self._bot_api_started_at: datetime | None = None
        # FR-CR-05-39 — periodic poll of the Fireflies API. Same
        # toggle pattern as the TG view poll above.
        self._fireflies_pipeline = None  # set via wire_fireflies()
        self._fireflies_realtime_enabled = False
        self._fireflies_poll_interval = 60
        self._fireflies_poll_batch = 20
        self._last_fireflies_poll_at = 0.0
        # FR-CR-05-51 — same «from-now» cutoff as the TG view
        # poll. Recordings whose `meeting_date` is before listener
        # startup are skipped so we don't backfill stale meetings
        # with a fresh deploy. Old meetings are still ingestible
        # via `ops.migrate_fireflies --newest --limit N` when
        # actually needed.
        self._fireflies_started_at: datetime | None = None
        # FR-CR-05-118 — same scaffolding for Zoom Cloud
        # Recordings. Wired via `wire_zoom()` from
        # `ops/telegram_listener.py`. Off until the operator sets
        # `ZOOM_REALTIME_ENABLED=true` in `.env`.
        self._zoom_pipeline = None
        self._zoom_realtime_enabled = False
        self._zoom_poll_interval = 60
        self._zoom_poll_batch = 10
        self._last_zoom_poll_at = 0.0
        self._zoom_started_at: datetime | None = None
        # FR-CR-05-61 — periodic Google Tasks pull. Wired via
        # `wire_google_tasks_pull()`; off until then so the
        # listener stays usable without Google Tasks configured.
        self._google_tasks_pull_factory: (
            Callable[[], "GoogleTasksPullService | None"] | None
        ) = None
        self._google_tasks_pull_interval = 60
        self._last_google_tasks_pull_at = 0.0

    def wire_google_tasks_pull(
        self,
        *,
        factory,
        poll_interval_seconds: int = 60,
    ) -> None:
        """FR-CR-05-61 — register the Google Tasks pull factory.
        Called once at startup from `ops/telegram_listener.py`.

        Pulls run on the same per-tick cadence as the other
        polls, throttled to `poll_interval_seconds` (default 60).
        No-op when factory is None."""
        self._google_tasks_pull_factory = factory
        self._google_tasks_pull_interval = max(0, int(poll_interval_seconds))

    def wire_fireflies(
        self,
        *,
        pipeline,
        enabled: bool,
        poll_interval_seconds: int,
        poll_batch_size: int,
    ) -> None:
        """Hook a configured FirefliesPipeline + the toggle from
        settings into the listener. Called from
        `ops/telegram_listener.py` at startup."""
        self._fireflies_pipeline = pipeline
        self._fireflies_realtime_enabled = bool(enabled)
        self._fireflies_poll_interval = max(0, int(poll_interval_seconds))
        self._fireflies_poll_batch = max(1, int(poll_batch_size))

    def wire_zoom(
        self,
        *,
        pipeline,
        enabled: bool,
        poll_interval_seconds: int,
        poll_batch_size: int,
    ) -> None:
        """FR-CR-05-118 — symmetric with `wire_fireflies`. Hooks
        the configured ZoomPipeline + toggle into the listener
        so realtime polling can pick up new Cloud Recordings on
        the same tick cadence."""
        self._zoom_pipeline = pipeline
        self._zoom_realtime_enabled = bool(enabled)
        self._zoom_poll_interval = max(0, int(poll_interval_seconds))
        self._zoom_poll_batch = max(1, int(poll_batch_size))

    @property
    def enabled(self) -> bool:
        return bool(self._token)

    def _maybe_run_sheet_pulls(self) -> None:
        """FR-CR-05-28 — pull operator edits from both Sheets
        into the DB on a schedule. Called on every tick; no-op
        unless ``_sheet_poll_interval`` seconds have elapsed
        since the last run.

        Each pull runs in its own session_scope so a transient
        Google Sheets HTTP error doesn't poison the listener
        transaction. Errors are logged and swallowed — the
        listener keeps processing Telegram updates either way.
        """
        if self._sheet_poll_interval <= 0:
            return
        now = time.time()
        if now - self._last_sheet_poll_at < self._sheet_poll_interval:
            return
        self._last_sheet_poll_at = now
        if self._team_sheet_factory is not None:
            try:
                sync = self._team_sheet_factory()
                if sync is not None:
                    with session_scope() as s:
                        updated, inserted = sync.pull(s)
                    if updated or inserted:
                        log.info(
                            "listener_team_sheet_pulled",
                            updated=updated,
                            inserted=inserted,
                        )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "listener_team_sheet_pull_failed", error=str(e)
                )
        if self._tasks_sheet_pull_factory is not None:
            try:
                sync = self._tasks_sheet_pull_factory()
                if sync is not None:
                    with session_scope() as s:
                        seen, changed, skipped = sync.pull(s)
                    if changed:
                        log.info(
                            "listener_tasks_sheet_pulled",
                            seen=seen,
                            changed=changed,
                            skipped=skipped,
                        )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "listener_tasks_sheet_pull_failed", error=str(e)
                )

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

    def _maybe_pull_google_tasks(self) -> None:
        """FR-CR-05-61 — periodically pull edits / deletes from
        Google Tasks back into the DB.

        On each pull cycle: every active task in the configured
        tasklist is fetched; title / notes / due / status are
        diffed against the matching DB row and written; deletes
        (rows in DB with `google_tasks_id` set but absent from
        the API list) flip `Task.deleted_at = now` and write a
        cancellation history row. After each change the task's
        TG card is refreshed so the operator sees fresh state in
        Telegram without leaving the bot.

        No-op until `wire_google_tasks_pull()` registers a
        factory (which itself only runs when
        `GOOGLE_TASKS_DEFAULT_TASKLIST_ID` is configured)."""
        if self._google_tasks_pull_factory is None:
            return
        if self._google_tasks_pull_interval <= 0:
            return
        now = time.time()
        if now - self._last_google_tasks_pull_at < self._google_tasks_pull_interval:
            return
        self._last_google_tasks_pull_at = now

        try:
            service = self._google_tasks_pull_factory()
        except Exception as e:  # noqa: BLE001
            log.warning(
                "listener_google_tasks_pull_factory_failed", error=str(e)
            )
            return
        if service is None:
            return

        # Build a closure that refreshes the editor's TG card —
        # used by the pull service after each apply / delete.
        def _refresh(task: Task) -> None:
            from app.telegram_bot.cards import refresh_card, render_tombstone

            if task.deleted_at is not None:
                render_tombstone(
                    sender=self._sender,
                    task=task,
                    actor=None,
                    session=None,
                )
            else:
                # `refresh_card` opens its own session-aware
                # rendering; pass our current session via
                # closure scope by re-grabbing one. The pull
                # service holds a session for diffs but the TG
                # card render layer expects a session for owner-
                # link resolution — open a fresh one.
                with session_scope() as s2:
                    fresh = s2.get(Task, task.id)
                    if fresh is not None:
                        refresh_card(
                            sender=self._sender,
                            session=s2,
                            task=fresh,
                        )

        try:
            with session_scope() as session:
                report = service.pull(session, refresh_card=_refresh)
            if report.updated or report.deleted or report.errors:
                log.info(
                    "listener_google_tasks_pull_done",
                    seen=report.seen,
                    updated=report.updated,
                    deleted=report.deleted,
                    status_changed=report.status_changed,
                    errors=report.errors,
                )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "listener_google_tasks_pull_failed", error=str(e)
            )

    def _maybe_poll_fireflies(self) -> None:
        """FR-CR-05-39 — periodically pull new recordings from
        Fireflies and run them through the full pipeline.
        Disabled unless ``FIREFLIES_REALTIME_ENABLED=true`` and
        a pipeline has been wired via `wire_fireflies()`."""
        if not self._fireflies_realtime_enabled:
            return
        if self._fireflies_poll_interval <= 0:
            return
        if self._fireflies_pipeline is None:
            return
        now = time.time()
        if now - self._last_fireflies_poll_at < self._fireflies_poll_interval:
            return
        self._last_fireflies_poll_at = now

        if self._fireflies_started_at is None:
            self._fireflies_started_at = datetime.now(timezone.utc)
        try:
            transcripts = self._fireflies_pipeline._client.list_transcripts(
                limit=self._fireflies_poll_batch
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "listener_fireflies_poll_list_failed", error=str(e)
            )
            return
        if not transcripts:
            return
        processed = 0
        skipped = 0
        skipped_old = 0
        errors = 0
        tasks_total = 0
        cutoff = self._fireflies_started_at
        for t in transcripts:
            # FR-CR-05-51 — drop recordings finished before
            # listener startup. tz-aware compare; treat naive as
            # UTC.
            mt = getattr(t, "meeting_date", None)
            if mt is not None:
                if mt.tzinfo is None:
                    mt = mt.replace(tzinfo=timezone.utc)
                if cutoff is not None and mt < cutoff:
                    skipped_old += 1
                    continue
            try:
                with session_scope() as session:
                    report = self._fireflies_pipeline.process_one(session, t)
                if report.skipped_reason:
                    skipped += 1
                else:
                    processed += 1
                    tasks_total += report.tasks_created
            except Exception as e:  # noqa: BLE001
                errors += 1
                log.warning(
                    "listener_fireflies_poll_recording_failed",
                    fireflies_id=t.id,
                    error=str(e),
                )
        if processed or errors:
            log.info(
                "listener_fireflies_poll_done",
                seen=len(transcripts),
                processed=processed,
                skipped=skipped,
                tasks_created=tasks_total,
                errors=errors,
            )

    def _maybe_poll_zoom(self) -> None:
        """FR-CR-05-118 — periodic poll for new Zoom Cloud
        Recordings. Symmetric to `_maybe_poll_fireflies`:
        throttled by `_zoom_poll_interval`, off until
        `wire_zoom()` was called with `enabled=True`, idempotent
        (already-processed `zoom_id`s short-circuit on per-step
        flags inside `ZoomPipeline.process_one`).

        Recordings whose `meeting_date` is older than the
        listener's startup time are skipped so a fresh deploy
        doesn't backfill stale meetings — same FR-CR-05-51
        cutoff used for Fireflies."""
        if not self._zoom_realtime_enabled:
            return
        if self._zoom_poll_interval <= 0:
            return
        if self._zoom_pipeline is None:
            return
        now = time.time()
        if now - self._last_zoom_poll_at < self._zoom_poll_interval:
            return
        self._last_zoom_poll_at = now

        if self._zoom_started_at is None:
            self._zoom_started_at = datetime.now(timezone.utc)
        try:
            metas = self._zoom_pipeline._client.list_recordings(  # noqa: SLF001
                limit=self._zoom_poll_batch
            )
        except Exception as e:  # noqa: BLE001
            log.warning("listener_zoom_poll_list_failed", error=str(e))
            return
        if not metas:
            return
        processed = 0
        skipped = 0
        skipped_old = 0
        errors = 0
        tasks_total = 0
        cutoff = self._zoom_started_at
        for m in metas:
            mt = getattr(m, "meeting_date", None)
            if mt is not None:
                if mt.tzinfo is None:
                    mt = mt.replace(tzinfo=timezone.utc)
                if cutoff is not None and mt < cutoff:
                    skipped_old += 1
                    continue
            try:
                with session_scope() as session:
                    report = self._zoom_pipeline.process_one(session, m)
                if report.skipped_reason:
                    skipped += 1
                else:
                    processed += 1
                    tasks_total += report.tasks_created
            except Exception as e:  # noqa: BLE001
                errors += 1
                log.warning(
                    "listener_zoom_poll_recording_failed",
                    zoom_id=m.id,
                    error=str(e),
                )
        if processed or errors:
            log.info(
                "listener_zoom_poll_done",
                seen=len(metas),
                processed=processed,
                skipped=skipped,
                tasks_created=tasks_total,
                errors=errors,
            )

    def _maybe_poll_source_view(self) -> None:
        """FR-CR-05-35 / FR-CR-05-36 — periodically pull the
        freshest messages from the Supabase TG view and run them
        through `prepare_drafts` + `post_draft_confirmation`.

        Strategy: pull `view_poll_batch_size` newest rows in one
        SQL roundtrip (default 500 — large enough to cover
        bursts), iterate from newest to oldest, short-circuit on
        the FR-CR-04-26 per-message bookmark for already-processed
        rows. New rows produce widgets via
        `post_draft_confirmation` exactly as the historical
        migrator does.

        Doesn't try to be clever about pagination: if a deploy
        ever sees more than `batch_size` new messages in the
        poll interval, bump `VIEW_POLL_BATCH_SIZE` or run
        `ops.migrate_telegram_history` to catch up.

        Disabled unless ``VIEW_REALTIME_ENABLED=true`` is set in
        the environment. When the listener has no reader (no
        Supabase URL configured), the call is a no-op.
        """
        if not self._view_realtime_enabled:
            return
        if self._view_poll_interval <= 0:
            return
        reader = getattr(self._ingest, "_reader", None)
        if reader is None or not getattr(reader, "configured", False):
            return
        now = time.time()
        if now - self._last_view_poll_at < self._view_poll_interval:
            return
        self._last_view_poll_at = now
        # FR-CR-05-51 — first poll seeds the «process from now»
        # cutoff. Messages older than this are silently skipped
        # so the listener doesn't backfill history.
        if self._view_realtime_started_at is None:
            self._view_realtime_started_at = datetime.now(timezone.utc)

        from app.telegram_bot.cards import post_draft_confirmation

        proposed = nothing = errors = skipped_old = 0
        try:
            messages = list(
                reader.iter_newest(limit=self._view_poll_batch)
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "listener_view_poll_iter_failed", error=str(e)
            )
            return
        cutoff = self._view_realtime_started_at
        for msg in messages:
            # FR-CR-05-51 — drop everything strictly older than
            # listener startup. `sent_at` may be naive; coerce to
            # tz-aware UTC for the comparison.
            if msg.sent_at is not None:
                msg_at = msg.sent_at
                if msg_at.tzinfo is None:
                    msg_at = msg_at.replace(tzinfo=timezone.utc)
                if cutoff is not None and msg_at < cutoff:
                    skipped_old += 1
                    continue
            try:
                with session_scope() as session:
                    drafts = self._ingest.prepare_drafts(session, msg)
                    if not drafts:
                        nothing += 1
                        continue
                    for d in drafts:
                        payload = d.payload or {}
                        try:
                            post_draft_confirmation(
                                sender=self._sender,
                                session=session,
                                draft=d,
                                source_chat_id=msg.chat_id,
                                source_message_id=msg.message_id,
                                author_user_id=(
                                    str(msg.user_id) if msg.user_id else None
                                ),
                                owner_user_id=payload.get("owner_user_id"),
                            )
                        except Exception as e:  # noqa: BLE001
                            log.warning(
                                "listener_view_poll_widget_failed",
                                draft_id=d.id,
                                error=str(e),
                            )
                        proposed += 1
            except Exception as e:  # noqa: BLE001
                errors += 1
                log.warning(
                    "listener_view_poll_message_failed",
                    chat_id=msg.chat_id,
                    message_id=msg.message_id,
                    error=str(e),
                )
        if proposed or errors or skipped_old:
            log.info(
                "listener_view_poll_done",
                seen=len(messages),
                drafts_proposed=proposed,
                no_action_or_dedup=nothing,
                skipped_pre_startup=skipped_old,
                errors=errors,
            )

    def tick(self) -> ListenerReport:
        """Run one long-poll → process → save offset cycle.

        Each tick is its own DB transaction. If anything blows up
        mid-batch the offset isn't persisted, and the next tick
        re-fetches the same range — `processed_telegram_messages`
        backstops dedup on retry.
        """
        # FR-CR-05-28 — fire scheduled Sheet → DB pulls. Runs on
        # every tick but throttled to `sheet_poll_interval_seconds`
        # internally, so the cost is bounded.
        self._maybe_run_sheet_pulls()
        # FR-CR-05-35 — fire scheduled Supabase view poll, also
        # throttled internally.
        self._maybe_poll_source_view()
        # FR-CR-05-39 — Fireflies poll on the same tick.
        self._maybe_poll_fireflies()
        # FR-CR-05-118 — Zoom poll on the same tick.
        self._maybe_poll_zoom()
        # FR-CR-05-61 — Google Tasks pull on the same tick
        # (also throttled internally).
        self._maybe_pull_google_tasks()

        # FR-CR-05-51 — pin a «process from now» cutoff on first
        # tick. Bot API getUpdates can replay up to 24h of
        # undelivered updates after a cold start (or when the
        # listener bookmark was wiped); we don't want those
        # ancient messages spawning fresh task cards.
        if self._bot_api_started_at is None:
            self._bot_api_started_at = datetime.now(timezone.utc)

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

                # FR-CR-05-51 — drop pre-startup messages so a
                # cold start with a fresh DB doesn't spawn cards
                # for the last 24h of group chatter Telegram is
                # holding for us. Naive `sent_at` coerced to UTC.
                if (
                    self._bot_api_started_at is not None
                    and msg.sent_at is not None
                ):
                    msg_at = msg.sent_at
                    if msg_at.tzinfo is None:
                        msg_at = msg_at.replace(tzinfo=timezone.utc)
                    if msg_at < self._bot_api_started_at:
                        report.skipped_pre_startup += 1
                        continue

                # FR-CR-05-07 — upsert the sender into the
                # `telegram_chat_members` registry so the classifier
                # can resolve mentions like «Валя сделай X» against
                # real numeric user_ids on subsequent messages. The
                # `has_started_bot=True` flag is set here whenever
                # the chat is a private DM with the bot — that's
                # the only kind of message that proves the user has
                # /started us.
                try:
                    _upsert_member_from_update(session, upd)
                except Exception as e:  # noqa: BLE001
                    log.info("telegram_member_upsert_failed", error=str(e))

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
                    # FR-CR-05-45 — welcome widget on /start (and /help).
                    # Operators arrive at the bot's DM cold and need a
                    # one-screen pitch: «Запиши задачи текстом или
                    # голосом — я разберу. Можно списком». No buttons
                    # — the conversation IS the UI from message #1.
                    text_stripped = (msg.text or "").strip()
                    if msg.is_private and text_stripped in (
                        "/start", "/help", "/start@",
                    ):
                        try:
                            self._sender.send_message(
                                chat_id=msg.chat_id,
                                text=_WELCOME_WIDGET_TEXT,
                            )
                        except Exception as e:  # noqa: BLE001
                            log.info(
                                "telegram_welcome_send_failed",
                                error=str(e),
                            )
                        continue

                    # FR-CR-05-44 — top-level voice / audio capture.
                    # The Edit/Done reply path (above) already
                    # transcribes via Whisper; without this branch a
                    # voice message in a private DM that's NOT a
                    # reply to a prompt would fall through with
                    # `msg.text == ""` and hit `process_all` /
                    # `prepare_drafts` as a silent no-op (or a
                    # downstream crash). We transcribe in-place and
                    # rebuild the message dataclass with the text
                    # filled in so the rest of the pipeline sees a
                    # normal text capture.
                    if not (msg.text or "").strip():
                        transcribed = self._maybe_transcribe_voice(msg)
                        if transcribed:
                            from dataclasses import replace as _replace

                            msg = _replace(msg, text=transcribed)
                        elif msg.is_private:
                            # Private DM, no text and no transcript
                            # — be polite, tell the user explicitly
                            # so they don't think the bot ate their
                            # voice silently.
                            try:
                                self._sender.send_message(
                                    chat_id=msg.chat_id,
                                    text=(
                                        "🎙 Couldn't transcribe the voice. "
                                        "Попробуй ещё раз или напиши "
                                        "текстом."
                                    ),
                                    reply_to_message_id=msg.message_id,
                                )
                            except Exception as e:  # noqa: BLE001
                                log.info(
                                    "telegram_voice_nudge_failed",
                                    error=str(e),
                                )
                            continue
                    # FR-CR-04-32 ext: when the author *explicitly*
                    # @-mentioned a teammate, intent is unambiguous —
                    # skip the «Create this task?» widget and fall
                    # through to immediate-create. Same shortcut as
                    # private chats.
                    has_explicit_mention = _has_at_mention(msg.text)
                    if msg.is_private or has_explicit_mention:
                        # 1:1 DM with the bot — or an explicit @
                        # mention in a group — the user is asking
                        # us directly, so create EVERY task in the
                        # message immediately (FR-CR-05-05) and DM
                        # one live card per task. (FR-CR-04-29 /
                        # FR-CR-04-32 ext.)
                        tasks = self._ingest.process_all(session, msg)
                        report.messages_processed += 1
                        if not tasks:
                            report.no_action += 1
                        else:
                            report.tasks_created += len(tasks)
                            for task in tasks:
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
                    else:
                        # Group / supergroup / channel — defer task
                        # creation. One ActionDraft + widget per task
                        # in the message (FR-CR-04-32 + FR-CR-05-05).
                        drafts = self._ingest.prepare_drafts(session, msg)
                        report.messages_processed += 1
                        if not drafts:
                            report.no_action += 1
                        else:
                            report.drafts_proposed += len(drafts)
                            for draft in drafts:
                                try:
                                    payload = draft.payload or {}
                                    post_draft_confirmation(
                                        sender=self._sender,
                                        session=session,
                                        draft=draft,
                                        source_chat_id=msg.chat_id,
                                        source_message_id=msg.message_id,
                                        author_user_id=(
                                            str(msg.user_id) if msg.user_id else None
                                        ),
                                        owner_user_id=payload.get("owner_user_id"),
                                    )
                                except Exception as e:  # noqa: BLE001
                                    log.warning(
                                        "telegram_post_draft_confirmation_failed",
                                        draft_id=draft.id,
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
            outcome = self._dispatch_action(
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
        if outcome is None:
            return

        # FR-CR-04-32 — Confirm / Reject re-render every draft widget
        # in place. Returned tuple is `(task_or_none, draft)`.
        if action == ACTION_CONFIRM:
            task, draft = outcome  # type: ignore[misc]
            if task is not None and draft is not None:
                replace_widgets_with_task_card(
                    sender=self._sender,
                    session=session,
                    draft=draft,
                    task=task,
                )
            return
        if action == ACTION_IGNORE:
            draft = outcome  # type: ignore[assignment]
            if draft is not None:
                render_draft_rejected(
                    sender=self._sender, draft=draft, actor=actor, session=session
                )
            return

        # Anything else returned a Task — re-render the regular card.
        task = outcome  # type: ignore[assignment]
        if task is None:
            return
        if action == ACTION_DELETE:
            render_tombstone(
                sender=self._sender, task=task, actor=actor, session=session
            )
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
            # FR-CR-04-32: Edit on the confirm widget targets a draft,
            # not a Task. Open the LLM-driven draft-edit conversation.
            if _looks_like_confirm_widget(cq):
                return self._open_edit_draft_conversation(
                    session, draft_id=entity_id, actor=actor, cq=cq
                )
            # FR-CR-04-29: free-form Edit on a real Task.
            return self._open_edit_conversation(
                session, task_id=entity_id, actor=actor, cq=cq
            )
        if action == ACTION_CONFIRM:
            # FR-CR-04-32 — finalise the draft into a Task. Returns a
            # `(task, draft)` tuple so the caller can replace each
            # widget DM with the regular task card.
            return tg_handlers.handle_confirm_draft(
                session, draft_id=entity_id, actor=actor
            )
        if action == ACTION_IGNORE:
            return tg_handlers.handle_ignore_draft(
                session, draft_id=entity_id, actor=actor
            )
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
        """FR-CR-05-37 — click on Mark done = transition the task
        to ``done`` immediately. The artifact prompt that follows
        is purely OPTIONAL — the user can reply with a link /
        comment to attach as the completion artifact, or just
        ignore the message.

        The pending registration stays so a reply does land back
        on `_handle_pending_reply` and gets stored on the task.
        """
        # Pre-flight authorisation check (raises NotAuthorised on
        # failure — the listener catches it and toasts the user).
        task, prompt_text = tg_handlers.prompt_done(
            session, task_id=task_id, actor=actor
        )
        chat = (cq.get("message") or {}).get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return None

        # Transition immediately. Returns the (possibly-already-
        # done) task so we can refresh the card.
        try:
            task = tg_handlers.handle_done(
                session, task_id=task_id, actor=actor
            )
        except tg_handlers.NotAuthorised:
            raise
        if task is not None:
            try:
                refresh_card(
                    sender=self._sender,
                    session=session,
                    task=task,
                    viewer=actor,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("telegram_done_refresh_failed", error=str(e))

        # Optional artifact prompt — NOT a force_reply (the user
        # can ignore it). Pending registration is keyed on
        # `prompt_message_id` so a swipe-reply from the operator
        # still routes correctly.
        resp = self._sender.send_message(
            chat_id=chat_id,
            text=prompt_text,
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
        # Already refreshed the card above; nothing for the
        # caller to do.
        return None

    def _open_edit_draft_conversation(
        self,
        session: Session,
        *,
        draft_id: int,
        actor: str,
        cq: dict[str, Any],
    ) -> Task | None:
        """Edit-on-draft (FR-CR-04-32 ext): user tapped ✏ Edit on
        a confirm widget. Post a force-reply prompt with the draft's
        current values; the reply hits ``_handle_pending_reply`` with
        action=``edit_draft`` and is parsed by the same LLM helper
        that drives Edit-on-task."""
        draft, prompt_text = tg_handlers.prompt_edit_draft(
            session, draft_id=draft_id, actor=actor
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
                action="edit_draft",
                task_id=draft_id,
                chat_id=int(chat_id),
                user_id=int(actor),
                prompt_message_id=int(prompt_msg_id),
            )
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

    def _maybe_transcribe_voice(
        self, msg: TelegramSourceMessage
    ) -> str:
        """FR-CR-05-14 — if the user replied with a voice or audio
        message instead of text, download via Bot API + `getFile`
        and transcribe via Whisper. Returns the (possibly empty)
        text — caller decides what to do with «no usable input».

        The TelegramSourceMessage already carries a stripped `text`
        plus the raw update payload in `raw`. Voice messages have
        `voice: {file_id, ...}`; audio uploads have `audio:
        {file_id, mime_type, ...}`. Either field, or none.
        """
        text = (msg.text or "").strip()
        if text:
            return text
        raw = msg.raw or {}
        voice = raw.get("voice") if isinstance(raw, dict) else None
        audio = raw.get("audio") if isinstance(raw, dict) else None
        attachment = voice or audio
        if not isinstance(attachment, dict):
            return ""
        file_id = attachment.get("file_id")
        if not file_id:
            return ""
        from app.config import get_settings

        settings = get_settings()
        if not settings.openai_api_key:
            log.info("telegram_voice_transcribe_skipped_no_openai_key")
            return ""
        audio_bytes = self._sender.download_file_bytes(file_id=file_id)
        if not audio_bytes:
            return ""
        from app.services.transcription import transcribe_bytes

        mime = attachment.get("mime_type") or "audio/ogg"
        # Whisper expects a filename — Telegram voice uses .oga.
        filename = "voice.ogg" if (voice is not None) else "audio"
        transcript = transcribe_bytes(
            audio_bytes=audio_bytes,
            mimetype=mime,
            filename=filename,
            openai_api_key=settings.openai_api_key,
        )
        if transcript:
            log.info(
                "telegram_voice_transcribed",
                preview=transcript[:120],
                duration=attachment.get("duration"),
            )
        return (transcript or "").strip()

    def _handle_pending_reply(
        self,
        session: Session,
        pending,  # PendingQuestion
        msg: TelegramSourceMessage,
    ) -> None:
        """Apply the user's reply to a prompt. ``pending.action``
        decides whether to complete the task with an artifact or
        apply an edit payload.

        On any path that materially changed state we delete the
        prompt message itself (best-effort) so the DM stays tidy —
        the user shouldn't have to scroll past stale «Edit draft #N»
        prompts to read the refreshed card. The user's own reply
        message stays (the bot can't delete user messages in DMs).
        """
        actor = str(msg.user_id) if msg.user_id else None
        if not actor:
            return

        # FR-CR-05-14 — voice messages: transcribe upfront, then
        # treat the transcript as the user's reply text. Mutates a
        # local copy so downstream handlers see the resolved text.
        reply_text = (msg.text or "").strip()
        if not reply_text:
            transcribed = self._maybe_transcribe_voice(msg)
            if transcribed:
                reply_text = transcribed
        if not reply_text:
            # Neither text nor a useful transcript — nudge the user
            # so they know the silent voice didn't go through.
            self._sender.send_message(
                chat_id=msg.chat_id,
                text=(
                    "🎙 Couldn't transcribe the voice. Try again or "
                    "send text."
                ),
                reply_to_message_id=msg.message_id,
            )
            return

        def _drop_prompt() -> None:
            try:
                self._sender.delete_message(
                    chat_id=pending.chat_id,
                    message_id=pending.prompt_message_id,
                )
            except Exception as e:  # noqa: BLE001
                log.info(
                    "telegram_prompt_delete_failed",
                    chat_id=pending.chat_id,
                    message_id=pending.prompt_message_id,
                    error=str(e),
                )

        if pending.action == "artifact":
            task = tg_handlers.apply_done_artifact_reply(
                session,
                task_id=pending.task_id,
                actor=actor,
                reply_text=reply_text,
            )
            if task is not None:
                _drop_prompt()
                refresh_card(
                    sender=self._sender,
                    session=session,
                    task=task,
                    viewer=actor,
                )
        elif pending.action == "edit":
            # Pass the same LLM backend that drives intent extraction
            # so the user can write the Edit reply in free-form natural
            # language ("push the deadline to Friday, priority high").
            backend = self._llm_backend()
            task, applied = tg_handlers.apply_edit_reply_ex(
                session,
                task_id=pending.task_id,
                actor=actor,
                reply_text=reply_text,
                llm_backend=backend,
            )
            if task is None:
                return
            if not applied:
                # LLM couldn't extract anything actionable (e.g. user
                # typed a single word with no field hint). Send a
                # short hint instead of silently doing nothing.
                self._sender.send_message(
                    chat_id=msg.chat_id,
                    text=(
                        "🤔 Couldn't tell what to change. "
                        "Try something like: <i>«push the deadline "
                        "to Friday, priority high»</i>."
                    ),
                    reply_to_message_id=msg.message_id,
                )
                return
            _drop_prompt()
            # FR-CR-05-47 — replace the editor's stale card with a
            # fresh one posted under their reply. Other recipients'
            # cards are refreshed in place inside the helper (they
            # didn't trigger the edit, but still need accurate state).
            try:
                from app.telegram_bot.cards import replace_card_for_viewer

                replace_card_for_viewer(
                    sender=self._sender,
                    session=session,
                    task=task,
                    viewer_chat_id=msg.chat_id,
                    reply_to_message_id=msg.message_id,
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "telegram_edit_replace_card_failed",
                    task_id=task.id,
                    error=str(e),
                )
            # Optional clarification nudge for vague-owner edits
            # (operator typed «другого оунера» but no resolution).
            if "owner" not in applied:
                low = (reply_text or "").lower()
                vague = any(
                    m in low
                    for m in (
                        "другого оунер",
                        "другого ответствен",
                        "другую ответствен",
                        "another owner",
                    )
                )
                if vague:
                    try:
                        self._sender.send_message(
                            chat_id=msg.chat_id,
                            text=(
                                "🤔 Wanted to change the owner? "
                                "Tell me who exactly (name or @handle)."
                            ),
                            reply_to_message_id=msg.message_id,
                        )
                    except Exception:  # noqa: BLE001
                        pass
        elif pending.action == "edit_draft":
            # Edit-on-draft — user tapped ✏ Edit on a confirm widget.
            # Same LLM backend; on success re-render every widget DM
            # so author + admins see the updated preview.
            backend = self._llm_backend()
            draft, applied = tg_handlers.apply_edit_draft_reply(
                session,
                draft_id=pending.task_id,
                actor=actor,
                reply_text=reply_text,
                llm_backend=backend,
            )
            if draft is None:
                return
            if not applied:
                self._sender.send_message(
                    chat_id=msg.chat_id,
                    text=(
                        "🤔 Couldn't tell what to change. "
                        "Try: <i>«push deadline to Friday, priority "
                        "high»</i>. When ready, tap ✅ Accept on the "
                        "original widget."
                    ),
                    reply_to_message_id=msg.message_id,
                )
                return
            _drop_prompt()
            # FR-CR-05-80 — replace the editor's stale widget
            # with a fresh one posted under their reply. Other
            # recipients' widgets are refreshed in place inside
            # the helper. Crucially the helper persists the new
            # widget's message_id onto `draft.payload["_widgets"]`
            # so a subsequent Accept's `replace_widgets_with_
            # task_card` converts the new widget too — without
            # it the new widget stayed alive with stale buttons.
            try:
                from app.telegram_bot.cards import (
                    replace_draft_widget_for_viewer,
                )

                replace_draft_widget_for_viewer(
                    sender=self._sender,
                    draft=draft,
                    viewer_chat_id=msg.chat_id,
                    reply_to_message_id=msg.message_id,
                    session=session,
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "telegram_edit_draft_replace_widget_failed",
                    draft_id=draft.id,
                    error=str(e),
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
                    drafts_proposed=report.drafts_proposed,
                    callbacks=report.callbacks_handled,
                    pending_replies=report.pending_replies_handled,
                    no_action=report.no_action,
                    skipped=report.skipped_non_message,
                    errors=report.errors,
                )
