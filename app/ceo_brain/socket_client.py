"""FR-CB2-1.1/1.2/1.3 — Slack Socket Mode client helpers.

Thin layer over ``slack_sdk.socket_mode`` that:
  * Opens a Socket Mode session with the dedicated CEO Brain
    tokens (FR-CB2-5.5).
  * Exposes the canonical list of event types we subscribe to.
  * Computes the exponential-backoff schedule for reconnects.
"""
from __future__ import annotations

from typing import Any, Callable

REQUIRED_EVENT_TYPES: tuple[str, ...] = (
    "app_mention",
    "message.channels",
    "message.groups",
    "message.im",
    "message.mpim",
)


def compute_reconnect_delays(max_attempts: int = 4) -> list[int]:
    """FR-CB2-1.3 — 2/4/8/16-second exponential backoff. Capped at
    ``max_attempts`` entries."""
    delays: list[int] = []
    for i in range(max_attempts):
        delays.append(2 ** (i + 1))
    return delays


def open_socket_connection(
    *,
    app_token: str | None = None,
    bot_token: str | None = None,
    client_factory: Callable[..., Any] | None = None,
) -> Any:
    """Return an opened Socket-Mode client handle. ``client_factory``
    is an injectable constructor for tests; when omitted we wire up
    the real ``slack_sdk.socket_mode.SocketModeClient``."""
    from app.ceo_brain.config import get_slack_tokens

    if app_token is None or bot_token is None:
        env_app, env_bot = get_slack_tokens()
        app_token = app_token or env_app
        bot_token = bot_token or env_bot

    if not app_token or not bot_token:
        return None

    if client_factory is None:  # pragma: no cover - real path
        try:
            from slack_sdk import WebClient
            from slack_sdk.socket_mode import SocketModeClient
        except ImportError:
            return None
        web = WebClient(token=bot_token)
        client = SocketModeClient(app_token=app_token, web_client=web)
        client.connect()
        return client

    return client_factory(app_token=app_token, bot_token=bot_token)


__all__ = [
    "REQUIRED_EVENT_TYPES",
    "compute_reconnect_delays",
    "open_socket_connection",
]
