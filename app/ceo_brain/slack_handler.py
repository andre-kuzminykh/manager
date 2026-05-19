"""FR-CB2-1.x wire-up: CEO Brain Slack listeners.

Two modes:

1. **Piggyback** — when ``CEO_BRAIN_SLACK_*_TOKEN`` env vars are
   absent OR identical to the project-wide ``SLACK_*_TOKEN``,
   the handlers are registered on the existing slack-task-bot
   Bolt ``App`` (zero extra connections; both features see every
   event).

2. **Standalone** — when CEO Brain has dedicated tokens (i.e. it
   lives in a *different* workspace than the task bot), spin up a
   second Bolt ``App`` + Socket-Mode handler in its own daemon
   thread so events from the right workspace actually land.

Operator pin 2026-05-18: «мне не надо новый app создавать, мне в
текущем надо» (humanoidheadquarters) — the standalone path
applies because the project's main bot lives in another
workspace.
"""
from __future__ import annotations

import threading
from typing import Any

from app.ceo_brain.cache import set_channel_fetcher, set_user_fetcher
from app.ceo_brain.config import get_archive_dir, get_slack_tokens
from app.ceo_brain.dispatcher import handle_event
from app.ceo_brain.responder import (
    post_placeholder,
    run_responder,
)
from app.config import Settings
from app.db import session_scope
from app.logging_setup import get_logger

log = get_logger(__name__)


def _build_thread_history_from_replies(
    replies: list[dict[str, Any]],
    *,
    bot_user_id: str | None,
) -> list[dict[str, Any]]:
    """FR-CB2-3.20 — assemble responder thread context from a Slack
    `conversations.replies` payload, **skipping bot-authored
    messages**. Operator-observed bug 2026-05-19: the bot's own
    `🤔 думаю…` placeholder was fetched back as part of the thread
    and added to history as `role=user`, so the model saw "🤔" as
    the latest user message and replied «получил только эмодзи».

    A message is bot-authored when EITHER:
      * `user == bot_user_id` (regular bot message), OR
      * `bot_id` is present (relay / app-posted message without a
        user ID).
    Empty-text messages are also skipped.
    """
    out: list[dict[str, Any]] = []
    for m in (replies or []):
        if not isinstance(m, dict):
            continue
        text = (m.get("text") or "").strip()
        if not text:
            continue
        if bot_user_id and m.get("user") == bot_user_id:
            continue
        if m.get("bot_id"):
            continue
        out.append({"role": "user", "content": text})
    return out


# FR-CB2-1.7 — module-level singleton tracking for the history
# poller. The Socket-Mode supervisor loop in
# `start_standalone_ceo_brain_bot` re-calls `_attach_handlers` on
# every reconnect; without this guard the poller daemon thread
# would accumulate (one extra per reconnect) and dispatch each
# Slack event N times → operator sees duplicate bot replies.
_history_poller_lock = threading.Lock()
_history_poller_started_for: set[tuple[str, ...]] = set()
# Late-bound for tests so they can swap the implementation.
try:  # pragma: no cover - import-time fallback
    from app.ceo_brain.history_poller import (
        SlackHistoryPoller,
        discover_operator_dm_channels,
    )
except ImportError:  # pragma: no cover
    SlackHistoryPoller = None  # type: ignore[assignment]
    discover_operator_dm_channels = None  # type: ignore[assignment]


def _reset_history_poller_singleton() -> None:
    """Test hook — wipe singleton state between tests."""
    with _history_poller_lock:
        _history_poller_started_for.clear()


def start_history_poller_singleton(
    *,
    slack_client: Any,
    bot_user_id: str | None,
    responder: Any,
    archive_dir: Any,
    settings: Settings,
) -> None:
    """Spawn the history poller exactly once per process per
    channel-set. Repeated calls (e.g. from the Socket-Mode reconnect
    supervisor) are no-ops once a poller for that channel-set is
    already running."""
    if SlackHistoryPoller is None or discover_operator_dm_channels is None:
        return
    channels = discover_operator_dm_channels(settings)
    if not channels:
        return
    key = tuple(sorted(channels))
    with _history_poller_lock:
        if key in _history_poller_started_for:
            log.info(
                "ceo_brain_history_poller_already_running",
                channels=list(key),
            )
            return
        _history_poller_started_for.add(key)
    SlackHistoryPoller(
        slack_client=slack_client,
        bot_user_id=bot_user_id,
        channels=list(key),
        responder=responder,
        archive_dir=archive_dir,
        settings=settings,
    ).start()


def _build_responder_callback(
    *,
    settings: Settings,
    slack_client: Any,
    bot_user_id: str | None,
):
    """Daemon-threaded callable suitable for the dispatcher's
    ``responder=`` parameter."""
    try:
        from anthropic import Anthropic
    except ImportError:
        log.warning(
            "ceo_brain_anthropic_sdk_missing",
            hint="pip install anthropic",
        )
        return None
    api_key = settings.ceo_brain_anthropic_api_key
    if not api_key:
        return None
    anthropic_client = Anthropic(api_key=api_key)

    # FR-CB2-3.16 — local Slack tools need a `xoxp-` user client for
    # `search.messages` (Slack rejects bot tokens on that endpoint).
    # Bot-only ops (history/replies/post/lookup) use `slack_client`.
    slack_user_client: Any | None = None
    user_token = (settings.ceo_brain_slack_user_token or "").strip()
    if user_token:
        try:
            from slack_sdk import WebClient as _WebClient
            slack_user_client = _WebClient(token=user_token)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "ceo_brain_slack_user_client_init_failed", error=str(e),
            )

    def _responder(payload: dict[str, Any]) -> None:
        channel = payload.get("channel") or ""
        ts = payload.get("ts") or payload.get("event_ts") or ""
        # Operator-pinned 2026-05-19 (revision): «мне надо в треде
        # чтоб отвечал как было». Always reply in a thread —
        # operator's parent thread when they reply inside one,
        # otherwise a synthetic thread anchored on the operator's
        # message ts. Keeps Slack-DM clean (no answer-blob between
        # questions) and lets the operator review previous answers
        # by clicking each thread.
        thread_ts = payload.get("thread_ts") or ts
        parent_thread_ts = payload.get("thread_ts")
        if not channel:
            return

        def _run() -> None:
            placeholder_ts = post_placeholder(
                slack=slack_client,
                channel=channel,
                thread_ts=thread_ts,
            )
            if not placeholder_ts:
                return
            history: list[dict[str, Any]] = []
            # Only pull thread context when this message lives in
            # an actual thread; for a top-level DM message there's
            # no thread to fetch (would 404). The history then
            # falls back to just the message body, which is
            # plenty for a single-turn exchange.
            if parent_thread_ts:
                try:
                    resp = slack_client.conversations_replies(
                        channel=channel,
                        ts=parent_thread_ts,
                        limit=settings.ceo_brain_thread_context_msgs,
                    )
                    if resp.get("ok"):
                        # FR-CB2-3.20 — strip bot-authored messages
                        # (incl. our own `🤔 думаю…` placeholder we
                        # just posted above) so the operator's
                        # actual question stays the last user
                        # message in context.
                        history = _build_thread_history_from_replies(
                            resp.get("messages") or [],
                            bot_user_id=bot_user_id,
                        )
                except Exception as e:  # noqa: BLE001
                    log.info(
                        "ceo_brain_thread_fetch_failed",
                        error=str(e), channel=channel,
                    )
            if not history:
                history = [
                    {"role": "user", "content": payload.get("text") or "(?)"},
                ]

            with session_scope() as db:
                run_responder(
                    slack=slack_client,
                    anthropic_client=anthropic_client,
                    channel=channel,
                    placeholder_ts=placeholder_ts,
                    thread_history=history,
                    db_session=db,
                    slack_event_ts=ts,
                    slack_bot_client=slack_client,
                    slack_user_client=slack_user_client,
                    placeholder_thread_ts=thread_ts,
                )

        threading.Thread(
            target=_run,
            name="ceo-brain-responder-run",
            daemon=True,
        ).start()

    return _responder


def _attach_handlers(app: Any, settings: Settings) -> None:
    """Wire `@app.event("message")` + `@app.event("app_mention")`
    on the given Bolt ``App``. Shared between piggyback and
    standalone modes."""
    slack_client = app.client

    def _users_info(user_id: str) -> dict:
        try:
            r = slack_client.users_info(user=user_id)
            return r.data if hasattr(r, "data") else dict(r)
        except Exception as e:  # noqa: BLE001
            log.info(
                "ceo_brain_users_info_failed",
                user_id=user_id, error=str(e),
            )
            return {}

    def _conv_info(channel_id: str) -> dict:
        try:
            r = slack_client.conversations_info(channel=channel_id)
            return r.data if hasattr(r, "data") else dict(r)
        except Exception as e:  # noqa: BLE001
            log.info(
                "ceo_brain_conv_info_failed",
                channel_id=channel_id, error=str(e),
            )
            return {}

    set_user_fetcher(_users_info)
    set_channel_fetcher(_conv_info)

    try:
        auth = slack_client.auth_test()
        bot_user_id = auth.get("user_id")
        bot_team = auth.get("team")
    except Exception as e:  # noqa: BLE001
        log.warning("ceo_brain_auth_test_failed", error=str(e))
        bot_user_id = None
        bot_team = None

    responder = (
        None if settings.ceo_brain_archive_only else
        _build_responder_callback(
            settings=settings,
            slack_client=slack_client,
            bot_user_id=bot_user_id,
        )
    )
    archive_dir = get_archive_dir()

    def _handle(payload: dict[str, Any]) -> None:
        try:
            with session_scope() as db:
                handle_event(
                    db, payload,
                    bot_user_id=bot_user_id,
                    responder=responder,
                    archive_dir=archive_dir,
                )
        except Exception as e:  # noqa: BLE001
            log.warning("ceo_brain_dispatch_failed", error=str(e))

    @app.event("message")  # type: ignore[misc]
    def _ceo_brain_on_message(event, ack):  # noqa: ANN001
        ack()
        _handle(event or {})

    @app.event("app_mention")  # type: ignore[misc]
    def _ceo_brain_on_mention(event, ack):  # noqa: ANN001
        ack()
        _handle(event or {})

    log.info(
        "ceo_brain_handlers_registered",
        archive_only=settings.ceo_brain_archive_only,
        bot_user_id=bot_user_id,
        team=bot_team,
    )

    # FR-CB2-1.7 — polling backstop. Slack Socket-Mode drops events
    # when a message is edited/deleted within ~1 sec of posting
    # (Slack collapses the wire), and during silent WebSocket
    # disconnects. The poller pulls `conversations.history` every
    # 1 sec and replays any unseen message through `_handle`, so
    # the responder fires reliably even when push fails.
    #
    # `start_history_poller_singleton` is idempotent: the supervisor
    # in `start_standalone_ceo_brain_bot` re-runs `_attach_handlers`
    # on every Socket-Mode reconnect, but only the first call here
    # actually spawns a poller (operator-confirmed dup-reply bug
    # 2026-05-19 fix).
    start_history_poller_singleton(
        slack_client=slack_client,
        bot_user_id=bot_user_id,
        responder=responder,
        archive_dir=archive_dir,
        settings=settings,
    )


def _needs_standalone(settings: Settings) -> bool:
    """Decide whether to spawn a dedicated Bolt App. True when
    CEO Brain tokens are EXPLICITLY set (different workspace);
    false when we should piggyback on the existing app."""
    cb_app, cb_bot = get_slack_tokens(settings)
    same_as_main = (
        cb_bot == settings.slack_bot_token
        and cb_app == settings.slack_app_token
    )
    if same_as_main:
        return False
    # Explicit override values present
    return bool(cb_bot)


def register_ceo_brain_handlers(app: Any, *, settings: Settings) -> None:
    """Piggyback path — attach CEO Brain listeners to the main
    slack-task-bot ``App``. Skipped when:

      * ``CEO_BRAIN_ENABLED=false``
      * Standalone mode is required (different workspace tokens)
    """
    if not settings.ceo_brain_enabled:
        log.info("ceo_brain_handler_disabled")
        return
    if _needs_standalone(settings):
        log.info(
            "ceo_brain_handler_standalone_required",
            hint=(
                "CEO_BRAIN_SLACK_*_TOKEN differs from main "
                "SLACK_*_TOKEN — handlers will be attached by "
                "start_standalone_ceo_brain_bot() instead"
            ),
        )
        return
    _attach_handlers(app, settings)


def start_standalone_ceo_brain_bot(*, settings: Settings) -> threading.Thread | None:
    """FR-CB2-5.5b — when CEO Brain lives in a different Slack
    workspace than the main task bot, spawn a second Bolt App +
    Socket-Mode handler with the dedicated tokens. Returns the
    daemon thread (or None if disabled / misconfigured)."""
    if not settings.ceo_brain_enabled:
        return None
    cb_app_token, cb_bot_token = get_slack_tokens(settings)
    if not cb_app_token or not cb_bot_token:
        log.warning(
            "ceo_brain_standalone_no_tokens",
            hint=(
                "set CEO_BRAIN_SLACK_APP_TOKEN + "
                "CEO_BRAIN_SLACK_BOT_TOKEN to spawn the dedicated "
                "Socket-Mode listener"
            ),
        )
        return None

    try:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError as e:
        log.warning(
            "ceo_brain_standalone_bolt_missing", error=str(e),
        )
        return None

    def _loop() -> None:
        """Supervisor — Slack closes idle WebSockets after a few
        hours and the inner client's auto-reconnect can fail
        silently. Wrap `SocketModeHandler.start()` in an infinite
        retry loop so the daemon recovers without needing a full
        container restart.

        Each attempt builds a FRESH Bolt App and a fresh handler;
        re-using a half-dead client is what causes the silent
        failure in the first place."""
        import time

        attempt = 0
        backoff_seconds = (2, 5, 10, 30, 60)
        while True:
            attempt += 1
            try:
                app = App(
                    token=cb_bot_token,
                    signing_secret=settings.ceo_brain_signing_secret or None,
                )
                _attach_handlers(app, settings)
                log.info(
                    "ceo_brain_standalone_socket_mode_starting",
                    attempt=attempt,
                )
                # `.start()` blocks forever under normal operation.
                # If it returns or raises we treat it as a dropped
                # connection and reconnect.
                SocketModeHandler(app, cb_app_token).start()
                log.warning(
                    "ceo_brain_standalone_socket_returned",
                    hint="SocketModeHandler.start() returned; reconnecting",
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "ceo_brain_standalone_socket_crashed",
                    error=str(e),
                    error_type=type(e).__name__,
                    attempt=attempt,
                )
            # Backoff before reconnect — slow ramp so we don't
            # hammer Slack if there's a service outage.
            delay = backoff_seconds[
                min(attempt - 1, len(backoff_seconds) - 1)
            ]
            time.sleep(delay)

    t = threading.Thread(
        target=_loop, name="ceo-brain-socket", daemon=True,
    )
    t.start()
    return t


__all__ = [
    "register_ceo_brain_handlers",
    "start_standalone_ceo_brain_bot",
]
