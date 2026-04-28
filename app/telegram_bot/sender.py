"""Outbound Telegram messaging.

Wraps the bare HTTP Bot API (``api.telegram.org/bot<token>/...``) so
we don't carry the python-telegram-bot async runtime into the rest
of the codebase. Three methods are enough for the MVP:

  - send_message(chat_id, text, reply_markup=...) → returns message_id
  - update_message(chat_id, message_id, text, reply_markup=...)
  - delete_message(chat_id, message_id)

A small `build_task_card_text` helper renders a Task into the same
visual shape as the Slack card (title, owner, due, priority, status)
but in plain Markdown — Telegram Bot API uses MarkdownV2 / HTML.
"""
from __future__ import annotations

import json
import urllib.error
from typing import Any

from app.logging_setup import get_logger
from app.models import Task

log = get_logger(__name__)

API_BASE = "https://api.telegram.org/bot"


PRIORITY_EMOJI = {
    "low": "🟢",
    "medium": "🟡",
    "high": "🟠",
    "urgent": "🔴",
}

STATUS_EMOJI = {
    "backlog": "📥",
    "todo": "📌",
    "in_progress": "🛠️",
    "done": "✅",
}


def _escape_md(text: str | None) -> str:
    """Escape the four characters Telegram's legacy Markdown parser
    treats as formatting tokens: ``_``, ``*``, ``\\``` and ``[``.

    Without this an owner whose Slack/Telegram username carries an
    underscore (very common — e.g. ``andre_andreevich``) trips the
    parser into thinking the rest of the line is italic, which
    fails with ``HTTP 400: can't parse entities``.
    """
    if not text:
        return ""
    return (
        text.replace("\\", "\\\\")
        .replace("_", "\\_")
        .replace("*", "\\*")
        .replace("`", "\\`")
        .replace("[", "\\[")
    )


def build_task_card_text(task: Task, *, header: str | None = None) -> str:
    """Render a Task as plain text suitable for `send_message` with
    ``parse_mode='Markdown'``.

    Static structural text uses ``*bold*`` for the title prefix, but
    every value that comes from user input or external profile data
    (title, description, owner display name) is escaped via
    ``_escape_md`` so a stray ``_`` / ``*`` / ``\\``` / ``[`` doesn't
    turn into an unbalanced formatting token.
    """
    lines: list[str] = []
    if header:
        lines.append(f"*{_escape_md(header)}*")
    lines.append(f"*#{task.id}* {_escape_md(task.title)}")
    if task.description:
        lines.append(_escape_md(task.description))
    meta: list[str] = []
    status_em = STATUS_EMOJI.get(task.status.value, "")
    meta.append(f"{status_em} {task.status.value}")
    owner = task.owner_display_name or task.owner_user_id
    if owner:
        meta.append(f"👤 {_escape_md(str(owner))}")
    pri_em = PRIORITY_EMOJI.get(task.priority.value, "")
    meta.append(f"{pri_em} {task.priority.value}")
    if task.due_date:
        meta.append(f"📅 {task.due_date.isoformat()}")
    if meta:
        lines.append(" · ".join(meta))
    if task.source_permalink:
        lines.append(f"🔗 {_escape_md(task.source_permalink)}")
    return "\n".join(lines)


class TelegramSender:
    """Thin synchronous wrapper around Telegram's Bot HTTP API.

    We use stdlib ``urllib`` rather than ``aiohttp`` / ``httpx`` so
    the sender works inside the existing synchronous Slack-Bolt event
    loop without an async bridge. Volumes are tiny (one message per
    task action), latency is fine.
    """

    def __init__(self, *, token: str, http_timeout: float = 10.0) -> None:
        self._token = token
        self._timeout = http_timeout
        # ``None`` token means "Telegram disabled" — every method
        # short-circuits to a no-op log line so calling code can stay
        # oblivious.
        self._enabled = bool(token)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _post(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self._enabled:
            log.debug("telegram_disabled_skipping", method=method)
            return {}

        # Inline imports so the module loads without `requests`/etc
        # available — useful for the test environment.
        import urllib.parse
        import urllib.request

        url = f"{API_BASE}{self._token}/{method}"
        # Booleans/ints stay raw; lists/dicts are JSON-encoded per
        # the Bot API spec.
        body: dict[str, str] = {}
        for k, v in params.items():
            if v is None:
                continue
            if isinstance(v, (dict, list)):
                body[k] = json.dumps(v, ensure_ascii=False)
            else:
                body[k] = str(v)
        data = urllib.parse.urlencode(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # Telegram returns the helpful `description` field on 4xx
            # too — read the body so we surface it instead of just
            # "HTTP Error 400: Bad Request".
            try:
                error_body = json.loads(e.read().decode("utf-8"))
                description = error_body.get("description", "")
            except Exception:  # noqa: BLE001
                description = ""
            log.warning(
                "telegram_api_call_failed",
                method=method,
                http_status=e.code,
                description=description,
            )
            return {}
        except Exception as e:  # noqa: BLE001
            log.warning("telegram_api_call_failed", method=method, error=str(e))
            return {}
        if not payload.get("ok"):
            log.warning(
                "telegram_api_returned_not_ok",
                method=method,
                description=payload.get("description"),
            )
            return {}
        return payload.get("result", {})

    def send_message(
        self,
        *,
        chat_id: int | str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = "Markdown",
    ) -> dict[str, Any]:
        """Returns the result body (with `message_id`) or empty dict
        on failure."""
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        if reply_to_message_id is not None:
            params["reply_to_message_id"] = reply_to_message_id
        if parse_mode:
            params["parse_mode"] = parse_mode
        return self._post("sendMessage", params)

    def update_message(
        self,
        *,
        chat_id: int | str,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = "Markdown",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        if parse_mode:
            params["parse_mode"] = parse_mode
        return self._post("editMessageText", params)

    def delete_message(self, *, chat_id: int | str, message_id: int) -> dict[str, Any]:
        return self._post(
            "deleteMessage", {"chat_id": chat_id, "message_id": message_id}
        )

    def forward_message(
        self,
        *,
        chat_id: int | str,
        from_chat_id: int | str,
        message_id: int,
    ) -> dict[str, Any]:
        """Forward a message from ``from_chat_id`` to ``chat_id``.

        Used by the FR-CR-04-32 confirm flow so the recipient sees the
        original message (with its sender attribution intact) above
        the "Create this task?" widget.
        """
        return self._post(
            "forwardMessage",
            {
                "chat_id": chat_id,
                "from_chat_id": from_chat_id,
                "message_id": message_id,
            },
        )

    def answer_callback_query(
        self, *, callback_query_id: str, text: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            params["text"] = text
        return self._post("answerCallbackQuery", params)
