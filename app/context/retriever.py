from dataclasses import dataclass, field
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class ContextWindow:
    conversation_id: str
    source_ts: str
    thread_ts: str | None
    source_message: dict[str, Any]
    history_before: list[dict[str, Any]] = field(default_factory=list)
    thread_messages: list[dict[str, Any]] = field(default_factory=list)

    def to_snapshot_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "source_ts": self.source_ts,
            "thread_ts": self.thread_ts,
            "source_message": self.source_message,
            "history_before": self.history_before,
            "thread_messages": self.thread_messages,
        }

    def flat_messages(self) -> list[dict[str, Any]]:
        """Return ordered messages for prompt building (oldest first)."""
        ordered = list(self.history_before) + [self.source_message]
        if self.thread_messages:
            # thread replies come *after* the source message when the source starts a thread;
            # otherwise they live in their own thread tree — we append them as tail context.
            ordered += [m for m in self.thread_messages if m.get("ts") != self.source_ts]
        return ordered


def _keep_fields(m: dict[str, Any]) -> dict[str, Any]:
    """Pick only the fields we actually need for downstream storage and prompting."""
    return {
        "ts": m.get("ts"),
        "thread_ts": m.get("thread_ts"),
        "user": m.get("user") or m.get("bot_id"),
        "text": m.get("text") or "",
        "subtype": m.get("subtype"),
    }


class ContextRetriever:
    """Loads a context window around a Slack message using conversations.history / replies."""

    def __init__(self, client: WebClient, window_before: int = 10) -> None:
        self._client = client
        self._window_before = window_before

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=5),
        retry=retry_if_exception_type(SlackApiError),
    )
    def _history(self, channel: str, latest: str, limit: int) -> list[dict[str, Any]]:
        resp = self._client.conversations_history(
            channel=channel, latest=latest, limit=limit, inclusive=False
        )
        return list(resp.get("messages", []))

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=5),
        retry=retry_if_exception_type(SlackApiError),
    )
    def _replies(self, channel: str, thread_ts: str) -> list[dict[str, Any]]:
        resp = self._client.conversations_replies(channel=channel, ts=thread_ts, limit=50)
        return list(resp.get("messages", []))

    def build(
        self,
        *,
        conversation_id: str,
        source_message: dict[str, Any],
    ) -> ContextWindow:
        source_ts: str = source_message["ts"]
        thread_ts: str | None = source_message.get("thread_ts")

        history_before: list[dict[str, Any]] = []
        try:
            raw_history = self._history(
                channel=conversation_id, latest=source_ts, limit=self._window_before
            )
            # Slack returns messages newest -> oldest; we want chronological order.
            history_before = [_keep_fields(m) for m in reversed(raw_history)]
        except SlackApiError as e:
            log.warning("conversations_history_failed", error=str(e), channel=conversation_id)

        thread_messages: list[dict[str, Any]] = []
        if thread_ts:
            try:
                raw_thread = self._replies(channel=conversation_id, thread_ts=thread_ts)
                thread_messages = [_keep_fields(m) for m in raw_thread]
            except SlackApiError as e:
                log.warning("conversations_replies_failed", error=str(e), thread_ts=thread_ts)

        return ContextWindow(
            conversation_id=conversation_id,
            source_ts=source_ts,
            thread_ts=thread_ts,
            source_message=_keep_fields(source_message),
            history_before=history_before,
            thread_messages=thread_messages,
        )
