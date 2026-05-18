"""FR-CB2-1.x wire-up: register CEO Brain handlers onto the
existing slack-task-bot Bolt ``App``.

Slack Bolt allows multiple handlers per event — adding a CEO
Brain ``@app.event("message")`` / ``@app.event("app_mention")``
alongside the task-bot's handlers means both features see every
event without code-sharing or token splitting.

Operator-pinned 2026-05-18: «мне не надо новый app создавать,
мне в текущем надо». Single Slack app, two consumers.
"""
from __future__ import annotations

import threading
from typing import Any

from app.ceo_brain.cache import set_channel_fetcher, set_user_fetcher
from app.ceo_brain.config import get_archive_dir
from app.ceo_brain.dispatcher import handle_event
from app.ceo_brain.responder import (
    post_placeholder,
    run_responder,
)
from app.config import Settings
from app.db import session_scope
from app.logging_setup import get_logger

log = get_logger(__name__)


def _build_responder_callback(
    *,
    settings: Settings,
    slack_client: Any,
    bot_user_id: str | None,
):
    """Return a callable suitable for the dispatcher's
    ``responder=`` parameter. The callable runs the full Claude
    responder pipeline in a daemon thread so the Bolt listener
    can return immediately."""
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

    def _responder(payload: dict[str, Any]) -> None:
        channel = payload.get("channel") or ""
        ts = payload.get("ts") or payload.get("event_ts") or ""
        thread_ts = payload.get("thread_ts") or ts
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
            # Pull thread context from Slack so the model sees the
            # surrounding conversation. Falls back to just the
            # @mention text if Slack returns nothing.
            history: list[dict[str, Any]] = []
            try:
                resp = slack_client.conversations_replies(
                    channel=channel,
                    ts=thread_ts,
                    limit=settings.ceo_brain_thread_context_msgs,
                )
                if resp.get("ok"):
                    for m in resp.get("messages") or []:
                        text = (m or {}).get("text") or ""
                        if text:
                            history.append({
                                "role": "user",
                                "content": text,
                            })
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
                )

        # Spawn so the Bolt event-loop isn't blocked while Claude
        # streams (can take ≥30 sec).
        threading.Thread(
            target=_run,
            name="ceo-brain-responder-run",
            daemon=True,
        ).start()

    return _responder


def register_ceo_brain_handlers(app: Any, *, settings: Settings) -> None:
    """Attach CEO Brain `message` / `app_mention` listeners onto a
    Bolt ``App``. Does NOTHING when ``CEO_BRAIN_ENABLED=false`` —
    so it's safe to call unconditionally from the build path."""
    if not settings.ceo_brain_enabled:
        log.info("ceo_brain_handler_disabled")
        return

    slack_client = app.client

    # Plug Slack lookups into the LRU caches so archive rows carry
    # human-readable channel + user names.
    def _users_info(user_id: str) -> dict:
        try:
            r = slack_client.users_info(user=user_id)
            return r.data if hasattr(r, "data") else dict(r)
        except Exception as e:  # noqa: BLE001
            log.info("ceo_brain_users_info_failed", user_id=user_id, error=str(e))
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

    # Capture the bot user id for the self-skip guard.
    try:
        auth = slack_client.auth_test()
        bot_user_id = auth.get("user_id")
    except Exception as e:  # noqa: BLE001
        log.warning("ceo_brain_auth_test_failed", error=str(e))
        bot_user_id = None

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

    # Subtype-less message — main archive path. Bolt fires `message`
    # for `message.channels` / `message.groups` / `message.im` /
    # `message.mpim` events. We register the listener; the dispatcher
    # decides what to do per event.
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
    )


__all__ = ["register_ceo_brain_handlers"]
