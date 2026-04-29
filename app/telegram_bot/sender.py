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
from app.models import Task, TaskSourceKind

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


def _escape_html(text: str | None) -> str:
    """Escape only the three characters Telegram's HTML parser treats
    as special: ``<``, ``>``, ``&``.

    HTML mode (vs. legacy Markdown) doesn't choke on underscores —
    so a username like ``andre_andreevich`` renders as plain text
    without needing backslash escapes that some Telegram clients
    show literally. Bold becomes ``<b>x</b>``, code becomes
    ``<code>x</code>``.
    """
    if not text:
        return ""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# Back-compat alias — older modules still import `_escape_md`.
# It's a thin shim onto the HTML escape now that we no longer use
# the Markdown parse mode.
_escape_md = _escape_html


def _format_owner(task: Task) -> str | None:
    """Return a presentation-friendly owner label.

    Prefers ``owner_display_name`` over the raw user id. For old
    Telegram tasks where the username was stored without ``@`` (the
    parse-update fix only applies to new captures), prefix it back —
    but only when it really looks like a Telegram handle (ASCII
    alnum + underscore, 5–32 chars, contains at least one letter, not
    all-digits — the latter is a numeric user_id, not a username).
    """
    import re

    raw = task.owner_display_name or task.owner_user_id
    if not raw:
        return None
    s = str(raw)
    if (
        task.source_kind == TaskSourceKind.telegram
        and not s.startswith("@")
        and re.fullmatch(r"[A-Za-z0-9_]{5,32}", s)
        and not s.isdigit()
        and any(c.isalpha() for c in s)
    ):
        s = f"@{s}"
    return s


_USERNAME_HANDLE_RE = __import__("re").compile(r"^@([A-Za-z][A-Za-z0-9_]{4,31})$")


def _resolve_owner_link_target(
    session,
    owner_user_id: str | None,
    owner_display_name: str | None,
) -> tuple[str | None, str | None]:
    """FR-CR-05-19 / FR-CR-05-20 — find the (`telegram_user_id`,
    `telegram_username`) pair to hyperlink the owner against.

    Two-table lookup:

      1. ``team_members`` by `owner_user_id` (numeric →
         `telegram_user_id`; otherwise → `slack_user_id`) or by
         display matched (case-insensitive) against
         `telegram_username` / `real_name`.
      2. ``telegram_chat_members`` (FR-CR-05-20) — when the team
         row is missing a `telegram_username` but we have a
         numeric TG id, the live listener has been observing this
         user across chats and likely captured their @-handle.
         Fall back to the most recent `username` for that
         `user_id`. Without this fallback half the registry only
         has numeric ids (auto-seed wrote sparse rows) and the
         widget can't emit a `t.me/<handle>` URL — `tg://user?id=`
         is silent for users who aren't in the bot's DM.

    Either or both fields may be ``None`` when neither source has
    that identity. Wrapped in try/except so a missing migration
    in the test fixture quietly degrades to «no extra info»."""
    if session is None:
        return None, None
    if not (owner_user_id or owner_display_name):
        return None, None
    try:
        from sqlalchemy import func, or_
        from app.models import TeamMember, TelegramChatMember

        row = None
        if owner_user_id:
            s = str(owner_user_id).strip()
            if s.isdigit():
                row = (
                    session.query(TeamMember)
                    .filter(TeamMember.telegram_user_id == int(s))
                    .first()
                )
            if row is None and s:
                row = (
                    session.query(TeamMember)
                    .filter(TeamMember.slack_user_id == s)
                    .first()
                )
        if row is None and owner_display_name:
            d = owner_display_name.strip().lstrip("@")
            if d:
                row = (
                    session.query(TeamMember)
                    .filter(
                        or_(
                            func.lower(TeamMember.telegram_username) == d.lower(),
                            func.lower(TeamMember.real_name) == d.lower(),
                        )
                    )
                    .first()
                )
        tg_id: str | None = None
        tg_handle: str | None = None
        if row is not None:
            tg_id = str(row.telegram_user_id) if row.telegram_user_id else None
            tg_handle = row.telegram_username or None
        # If we have a numeric id but no @handle, look up
        # telegram_chat_members — the listener writes usernames
        # there on every observed message.
        if tg_id and not tg_handle:
            try:
                chat_row = (
                    session.query(TelegramChatMember)
                    .filter(TelegramChatMember.user_id == int(tg_id))
                    .filter(TelegramChatMember.username.isnot(None))
                    .order_by(TelegramChatMember.last_seen_at.desc())
                    .first()
                )
                if chat_row and chat_row.username:
                    tg_handle = chat_row.username
            except Exception:  # noqa: BLE001
                pass
        return tg_id, tg_handle
    except Exception:  # noqa: BLE001
        return None, None


def _owner_html_link(
    owner_user_id: str | None,
    display: str,
    *,
    tg_user_id: str | None = None,
    tg_handle: str | None = None,
) -> str:
    """FR-CR-05-16 / FR-CR-05-18 / FR-CR-05-19 / FR-CR-05-20 —
    wrap `display` in a deeplink so a tap on the owner label
    opens a chat with them.

    Resolution chain (first match wins). PUBLIC `t.me/<handle>`
    URLs come first because `tg://user?id=<uid>` only renders as
    a clickable mention in clients where the tagged user is a
    member of the current chat — and the bot's DM with the
    operator obviously isn't shared with the owner. The public
    URL form works regardless of chat membership.

      1. ``tg_handle`` (registry / chat-members) →
         ``https://t.me/<handle>``.
      2. ``display`` matches ``@<handle>`` form →
         ``https://t.me/<handle>``.
      3. ``tg_user_id`` (registry-resolved numeric TG id) →
         ``tg://user?id=<uid>``. Last-ditch — works in some
         clients but not all when the user isn't in the chat.
      4. Numeric ``owner_user_id`` → ``tg://user?id=<uid>``.
      5. Otherwise → plain text.

    Callers that have a DB session should pass `tg_user_id` /
    `tg_handle` from `_resolve_owner_link_target`; callers
    without a session fall back to whatever can be inferred from
    `owner_user_id` / `display` directly. Display text is
    HTML-escaped; the wrapper element is the only raw HTML in
    the result.
    """
    safe = _escape_html(display)
    if tg_handle:
        clean = str(tg_handle).strip().lstrip("@")
        if clean:
            return f'<a href="https://t.me/{clean}">{safe}</a>'
    m = _USERNAME_HANDLE_RE.match((display or "").strip())
    if m is not None:
        return f'<a href="https://t.me/{m.group(1)}">{safe}</a>'
    if tg_user_id and str(tg_user_id).isdigit():
        return f'<a href="tg://user?id={tg_user_id}">{safe}</a>'
    s = str(owner_user_id) if owner_user_id else ""
    if s.isdigit():
        return f'<a href="tg://user?id={s}">{safe}</a>'
    return safe


def build_task_card_text(
    task: Task,
    *,
    header: str | None = None,
    session=None,
) -> str:
    """FR-CR-05-16 / FR-CR-05-18 / FR-CR-05-19 — minimal card
    layout, the title itself is the source-message hyperlink.

        {bullet} <a href="permalink"><b>title</b></a>
        📝 description
        👤 <owner-deeplink> · 📅 due-date

    `bullet` is the priority emoji (🟢/🟡/🟠/🔴) for open tasks,
    ✅ for done. Owner gets the strongest deeplink the registry
    can supply: numeric `tg://user?id=<uid>` is preferred, then
    `https://t.me/<handle>` (FR-CR-05-19 — looks up the
    `team_members` row when ``session`` is provided so registry-
    only handles still hyperlink), then `@handle`-in-display
    detection, otherwise plain text. The separate 🔗 line was
    rolled into the title — single-tap behaviour, less visual
    noise.

    No #id, no status word, no priority word — the colour /
    completion glyph carry the signal.
    """
    lines: list[str] = []
    if header:
        lines.append(f"<b>{_escape_html(header)}</b>")

    if task.status.value == "done":
        bullet = "✅"
    else:
        bullet = PRIORITY_EMOJI.get(task.priority.value, "🟡")
    safe_title = _escape_html(task.title or "")
    if task.source_permalink:
        title_html = (
            f'<a href="{_escape_html(task.source_permalink)}">'
            f"<b>{safe_title}</b></a>"
        )
    else:
        title_html = f"<b>{safe_title}</b>"
    lines.append(f"{bullet} {title_html}")

    if task.description:
        lines.append(f"📝 {_escape_html(task.description)}")

    meta: list[str] = []
    owner = _format_owner(task)
    if owner:
        tg_id, tg_handle = _resolve_owner_link_target(
            session, task.owner_user_id, task.owner_display_name or owner
        )
        meta.append(
            f"👤 {_owner_html_link(task.owner_user_id, owner, tg_user_id=tg_id, tg_handle=tg_handle)}"
        )
    if task.due_date:
        meta.append(f"📅 {task.due_date.isoformat()}")
    if meta:
        lines.append(" · ".join(meta))
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
        parse_mode: str | None = "HTML",
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
        parse_mode: str | None = "HTML",
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

    def get_file(self, *, file_id: str) -> dict[str, Any]:
        """Resolve a Telegram `file_id` to its `file_path` so the
        caller can fetch the bytes from
        ``https://api.telegram.org/file/bot<TOKEN>/<file_path>``.
        Used by the voice-message transcription path
        (FR-CR-05-14)."""
        return self._post("getFile", {"file_id": file_id})

    def download_file_bytes(
        self, *, file_id: str, max_bytes: int = 25 * 1024 * 1024
    ) -> bytes | None:
        """Two-step download: `getFile` + raw GET on the resolved
        URL. Returns ``None`` when the API rejects the file_id, when
        the file exceeds ``max_bytes`` (Whisper's per-request cap),
        or on any transport error.

        Telegram tokens are scoped to the bot, so every download is
        authorised by virtue of using the bot URL form."""
        if not self._enabled:
            return None
        info = self.get_file(file_id=file_id)
        path = info.get("file_path") if isinstance(info, dict) else None
        if not path:
            return None
        url = f"https://api.telegram.org/file/bot{self._token}/{path}"
        import urllib.error
        import urllib.request

        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as resp:
                content = resp.read(max_bytes + 1)
        except urllib.error.URLError as e:
            log.warning("telegram_download_failed", file_id=file_id, error=str(e))
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("telegram_download_failed", file_id=file_id, error=str(e))
            return None
        if len(content) > max_bytes:
            log.warning(
                "telegram_audio_too_large",
                file_id=file_id,
                bytes=len(content),
                cap=max_bytes,
            )
            return None
        return content

    def answer_callback_query(
        self, *, callback_query_id: str, text: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            params["text"] = text
        return self._post("answerCallbackQuery", params)
