"""FR-CB2-1.7 — Polling fallback for missed Slack events.

Socket-Mode silently drops events when:
  * a message is sent and immediately deleted within the
    pre-delivery window (Slack collapses to delete-only);
  * the WebSocket hits a transient network glitch and the
    underlying client's auto-reconnect can't replay missed
    events (Slack does not retain delivery state);
  * Slack's edge throttles event push to apps after rapid
    reconnects.

This poller backstops Socket-Mode by periodically pulling
`conversations.history` for the operator's DM channel (and any
extra channels listed in env). For every message it has not
seen, it synthesises a Slack-compatible payload and feeds it
into ``dispatcher.handle_event`` — the same path Bolt would
have taken, so archive + responder fire exactly once via the
existing dedup (UNIQUE(channel_id, ts)).
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from app.ceo_brain.config import (
    get_archive_channel_whitelist,
    get_archive_dir,
)
from app.ceo_brain.dispatcher import handle_event
from app.config import Settings, get_settings
from app.db import session_scope
from app.logging_setup import get_logger
from app.models import SlackMessageArchive

log = get_logger(__name__)


_DEFAULT_INTERVAL_SEC = 5
_DEFAULT_LOOKBACK_SEC = 600  # 10 min — covers reconnect gaps
_DEFAULT_PAGE_LIMIT = 30


def _coerce_text(message: dict[str, Any]) -> str:
    """Slack stores the user message body in ``text`` for top-level
    messages and inside ``message`` / ``previous_message`` blocks
    for edits / deletes — pick whichever has content."""
    direct = message.get("text") or ""
    if direct:
        return direct
    msg = message.get("message") or {}
    if isinstance(msg, dict) and msg.get("text"):
        return msg.get("text") or ""
    return ""


def _to_event_payload(
    *,
    channel: str,
    channel_type: str,
    bot_user_id: str | None,
    message: dict[str, Any],
) -> dict[str, Any] | None:
    """Convert a `conversations.history` message into a payload
    that ``dispatcher.handle_event`` accepts. Returns None for
    messages we should skip (bot's own messages, missing ts)."""
    ts = (message.get("ts") or "").strip()
    if not ts:
        return None
    user = message.get("user") or ""
    bot_id = message.get("bot_id") or ""
    if bot_user_id and user == bot_user_id:
        return None
    subtype = message.get("subtype")
    payload: dict[str, Any] = {
        "type": "message",
        "channel": channel,
        "channel_type": channel_type,
        "ts": ts,
        "event_ts": ts,
        "user": user or None,
        "bot_id": bot_id or None,
        "subtype": subtype,
        "text": _coerce_text(message),
        "thread_ts": message.get("thread_ts"),
    }
    return payload


class SlackHistoryPoller:
    """Pulls `conversations.history` for the operator DM channel
    every ``interval_sec`` seconds and dispatches unseen messages
    through the existing event handler. Also polls
    `conversations.replies` for any thread root seen recently —
    Slack's push of thread-reply `message.im` events is
    unreliable, history endpoint does not include thread replies,
    so the two together backstop both top-level and threaded
    user input."""

    # Maximum age of a thread root after which we stop polling
    # its replies. Threads older than this are unlikely to receive
    # new messages so dropping them keeps the active-set bounded.
    _THREAD_TTL_SEC = 60 * 60  # 1 hour

    def __init__(
        self,
        *,
        slack_client: Any,
        bot_user_id: str | None,
        channels: list[str],
        responder: Callable[[dict[str, Any]], None] | None = None,
        archive_dir: Any = None,
        interval_sec: int = _DEFAULT_INTERVAL_SEC,
        lookback_sec: int = _DEFAULT_LOOKBACK_SEC,
        page_limit: int = _DEFAULT_PAGE_LIMIT,
        settings: Settings | None = None,
    ) -> None:
        self._slack = slack_client
        self._bot_user_id = bot_user_id
        self._channels = [c for c in channels if c]
        self._responder = responder
        self._archive_dir = archive_dir
        self._interval_sec = max(2, int(interval_sec))
        self._lookback_sec = max(60, int(lookback_sec))
        self._page_limit = max(5, int(page_limit))
        self._settings = settings or get_settings()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Per-channel high-watermark — we only look at messages
        # newer than the most recent ts we've already processed.
        # Initialised from the archive on first poll.
        self._high_water: dict[str, str] = {}
        # Per-(channel, thread_root_ts) high-watermark for thread
        # replies. Keyed `f"{channel}:{thread_ts}"`. The set of
        # active threads is rebuilt each tick from archive activity.
        self._thread_high_water: dict[str, str] = {}

    def start(self) -> threading.Thread | None:
        if not self._channels:
            log.info(
                "ceo_brain_history_poller_no_channels",
                hint="set CEO_BRAIN_HISTORY_POLL_CHANNELS=Dxxxx,..."
                " or rely on the operator DM auto-detection",
            )
            return None
        self._thread = threading.Thread(
            target=self._loop,
            name="ceo-brain-history-poller",
            daemon=True,
        )
        self._thread.start()
        log.info(
            "ceo_brain_history_poller_started",
            interval_sec=self._interval_sec,
            channels=self._channels,
        )
        return self._thread

    def stop(self) -> None:
        self._stop.set()

    def _initial_high_water(self, channel: str) -> str:
        """Seed the high-watermark from the archive — the last ts
        we've already processed for this channel. Falls back to
        ``now - lookback_sec`` if the archive is empty."""
        try:
            with session_scope() as s:
                row = (
                    s.query(SlackMessageArchive)
                    .filter(SlackMessageArchive.channel_id == channel)
                    .order_by(SlackMessageArchive.ts.desc())
                    .first()
                )
                if row and row.ts:
                    return row.ts
        except Exception as e:  # noqa: BLE001
            log.info(
                "ceo_brain_history_poller_seed_failed",
                channel=channel, error=str(e),
            )
        return f"{int(time.time() - self._lookback_sec)}.000000"

    def _channel_type(self, channel: str) -> str:
        if channel.startswith("D"):
            return "im"
        if channel.startswith("G"):
            return "mpim"
        return "channel"

    def _poll_once(self, channel: str) -> int:
        if channel not in self._high_water:
            self._high_water[channel] = self._initial_high_water(channel)
        try:
            resp = self._slack.conversations_history(
                channel=channel,
                oldest=self._high_water[channel],
                inclusive=False,
                limit=self._page_limit,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "ceo_brain_history_poller_call_failed",
                channel=channel, error=str(e),
            )
            return 0
        if not resp.get("ok"):
            log.warning(
                "ceo_brain_history_poller_resp_not_ok",
                channel=channel, response=dict(resp),
            )
            return 0
        # `conversations.history` returns newest-first; replay
        # oldest-first so dispatcher sees the natural conversation
        # order.
        messages = list(reversed(resp.get("messages") or []))
        dispatched = 0
        new_high_water = self._high_water[channel]
        chan_type = self._channel_type(channel)
        for msg in messages:
            payload = _to_event_payload(
                channel=channel,
                channel_type=chan_type,
                bot_user_id=self._bot_user_id,
                message=msg,
            )
            if payload is None:
                if (msg or {}).get("ts"):
                    new_high_water = max(new_high_water, msg["ts"])
                continue
            try:
                with session_scope() as db:
                    handle_event(
                        db, payload,
                        bot_user_id=self._bot_user_id,
                        responder=self._responder,
                        archive_dir=self._archive_dir or get_archive_dir(),
                    )
                dispatched += 1
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "ceo_brain_history_poller_dispatch_failed",
                    channel=channel, ts=payload.get("ts"),
                    error=str(e),
                )
            ts = payload.get("ts")
            if ts:
                new_high_water = max(new_high_water, ts)
        self._high_water[channel] = new_high_water
        if dispatched:
            log.info(
                "ceo_brain_history_poller_dispatched",
                channel=channel, count=dispatched,
                high_water=new_high_water,
            )
        return dispatched

    def _active_thread_roots(self, channel: str) -> list[str]:
        """Threads we've seen activity in within
        ``_THREAD_TTL_SEC``. Looking at the archive: any row with
        a thread_ts whose newest descendant is recent counts."""
        cutoff = time.time() - self._THREAD_TTL_SEC
        try:
            with session_scope() as s:
                rows = (
                    s.query(SlackMessageArchive)
                    .filter(SlackMessageArchive.channel_id == channel)
                    .filter(SlackMessageArchive.thread_ts.isnot(None))
                    .order_by(SlackMessageArchive.ts.desc())
                    .limit(50)
                    .all()
                )
                seen: set[str] = set()
                for row in rows:
                    try:
                        if float(row.ts) < cutoff:
                            continue
                    except (TypeError, ValueError):
                        continue
                    if row.thread_ts:
                        seen.add(row.thread_ts)
                return sorted(seen)
        except Exception as e:  # noqa: BLE001
            log.info(
                "ceo_brain_active_threads_lookup_failed",
                channel=channel, error=str(e),
            )
            return []

    def _poll_thread_once(
        self, channel: str, thread_ts: str,
    ) -> int:
        key = f"{channel}:{thread_ts}"
        oldest = self._thread_high_water.get(key, thread_ts)
        try:
            resp = self._slack.conversations_replies(
                channel=channel,
                ts=thread_ts,
                oldest=oldest,
                inclusive=False,
                limit=self._page_limit,
            )
        except Exception as e:  # noqa: BLE001
            log.info(
                "ceo_brain_thread_poll_failed",
                channel=channel, thread_ts=thread_ts, error=str(e),
            )
            return 0
        if not resp.get("ok"):
            return 0
        messages = list(reversed(resp.get("messages") or []))
        dispatched = 0
        new_high = oldest
        for msg in messages:
            # Skip the parent of the thread itself (Slack includes
            # it in replies output).
            if (msg or {}).get("ts") == thread_ts:
                new_high = max(new_high, thread_ts)
                continue
            payload = _to_event_payload(
                channel=channel,
                channel_type=self._channel_type(channel),
                bot_user_id=self._bot_user_id,
                message=msg,
            )
            if payload is None:
                if (msg or {}).get("ts"):
                    new_high = max(new_high, msg["ts"])
                continue
            # Make sure thread_ts is set so dispatcher knows it's
            # a thread reply (conversations.replies items may or
            # may not include it).
            payload.setdefault("thread_ts", thread_ts)
            try:
                with session_scope() as db:
                    handle_event(
                        db, payload,
                        bot_user_id=self._bot_user_id,
                        responder=self._responder,
                        archive_dir=self._archive_dir or get_archive_dir(),
                    )
                dispatched += 1
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "ceo_brain_thread_poll_dispatch_failed",
                    channel=channel, ts=payload.get("ts"),
                    error=str(e),
                )
            ts = payload.get("ts")
            if ts:
                new_high = max(new_high, ts)
        self._thread_high_water[key] = new_high
        if dispatched:
            log.info(
                "ceo_brain_thread_poll_dispatched",
                channel=channel, thread_ts=thread_ts,
                count=dispatched,
            )
        return dispatched

    def _loop(self) -> None:
        # Brief stagger so we don't race the Socket-Mode startup.
        self._stop.wait(timeout=5)
        while not self._stop.is_set():
            for channel in list(self._channels):
                if self._stop.is_set():
                    break
                try:
                    self._poll_once(channel)
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "ceo_brain_history_poller_tick_failed",
                        channel=channel, error=str(e),
                    )
                # Thread-replies sub-poll.
                for thread_ts in self._active_thread_roots(channel):
                    if self._stop.is_set():
                        break
                    try:
                        self._poll_thread_once(channel, thread_ts)
                    except Exception as e:  # noqa: BLE001
                        log.warning(
                            "ceo_brain_thread_poll_tick_failed",
                            channel=channel,
                            thread_ts=thread_ts,
                            error=str(e),
                        )
            self._stop.wait(timeout=self._interval_sec)


def discover_operator_dm_channels(settings: Settings | None = None) -> list[str]:
    """Operator-pinned default: poll the brief target channel
    (``COUNTERPARTY_BRIEFS_SLACK_TARGET_CHANNEL_ID``) — that's the
    operator's CEO Brain DM. Plus the archive whitelist if set.
    """
    s = settings or get_settings()
    channels: set[str] = set()
    target = (
        s.counterparty_briefs_slack_target_channel_id
        or s.agenda_slack_target_channel_id
        or ""
    ).strip()
    if target:
        channels.add(target)
    channels.update(get_archive_channel_whitelist(s))
    return sorted(channels)


__all__ = [
    "SlackHistoryPoller",
    "discover_operator_dm_channels",
]
