"""Rate-aware Slack message sender.

Slack's chat.postMessage typically allows about one message per second per channel
and returns HTTP 429 with a Retry-After header on excess. This helper enforces a
per-channel minimum interval and transparently retries on 429.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from app.logging_setup import get_logger

log = get_logger(__name__)


class RateAwareSlackSender:
    def __init__(self, client: WebClient, min_interval_seconds: float = 1.0) -> None:
        self._client = client
        self._min_interval = min_interval_seconds
        self._last_sent: dict[str, float] = defaultdict(float)
        self._lock = threading.Lock()

    def _throttle(self, channel: str) -> None:
        with self._lock:
            now = time.monotonic()
            last = self._last_sent[channel]
            wait = (last + self._min_interval) - now
            if wait > 0:
                time.sleep(wait)
            self._last_sent[channel] = time.monotonic()

    def post_message(self, *, channel: str, **kwargs: Any) -> dict[str, Any]:
        attempts = 0
        while True:
            self._throttle(channel)
            try:
                return self._client.chat_postMessage(channel=channel, **kwargs).data  # type: ignore[return-value]
            except SlackApiError as e:
                status = e.response.status_code if e.response is not None else None
                if status == 429 and attempts < 3:
                    retry_after = int(e.response.headers.get("Retry-After", "1"))  # type: ignore[union-attr]
                    log.warning(
                        "slack_rate_limited",
                        channel=channel,
                        retry_after=retry_after,
                        attempt=attempts,
                    )
                    time.sleep(retry_after)
                    attempts += 1
                    continue
                raise

    def update_message(
        self, *, channel: str, ts: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Update an existing chat message (chat.update)."""
        attempts = 0
        while True:
            self._throttle(channel)
            try:
                return self._client.chat_update(
                    channel=channel, ts=ts, **kwargs
                ).data  # type: ignore[return-value]
            except SlackApiError as e:
                status = e.response.status_code if e.response is not None else None
                if status == 429 and attempts < 3:
                    retry_after = int(e.response.headers.get("Retry-After", "1"))  # type: ignore[union-attr]
                    time.sleep(retry_after)
                    attempts += 1
                    continue
                raise

    def post_ephemeral(
        self, *, channel: str, user: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Post an ephemeral message visible only to ``user``. Used for
        CR-03 admin-only thread notifications."""
        attempts = 0
        while True:
            self._throttle(channel)
            try:
                return self._client.chat_postEphemeral(
                    channel=channel, user=user, **kwargs
                ).data  # type: ignore[return-value]
            except SlackApiError as e:
                status = e.response.status_code if e.response is not None else None
                if status == 429 and attempts < 3:
                    retry_after = int(e.response.headers.get("Retry-After", "1"))  # type: ignore[union-attr]
                    time.sleep(retry_after)
                    attempts += 1
                    continue
                raise

    def delete_message(self, *, channel: str, ts: str) -> dict[str, Any]:
        """Delete an existing chat message (chat.delete)."""
        attempts = 0
        while True:
            self._throttle(channel)
            try:
                return self._client.chat_delete(channel=channel, ts=ts).data  # type: ignore[return-value]
            except SlackApiError as e:
                status = e.response.status_code if e.response is not None else None
                if status == 429 and attempts < 3:
                    retry_after = int(e.response.headers.get("Retry-After", "1"))  # type: ignore[union-attr]
                    time.sleep(retry_after)
                    attempts += 1
                    continue
                raise
