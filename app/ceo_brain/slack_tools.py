"""FR-CB2-3.16 — Local Slack tools for the CEO Brain responder.

Slack's public MCP server (`mcp.slack.com`) requires Anthropic-managed
OAuth — incompatible with the operator's plain `xoxb-` / `xoxp-`
tokens. Instead we expose the same surface as regular Anthropic
``tools`` and execute them locally via ``slack_sdk``: same observable
behaviour, no external server, no OAuth dance.

Module shape:
    - ``SLACK_TOOL_SCHEMAS`` — Anthropic tool definitions (name,
      description, input_schema). Passed verbatim to ``tools=[...]``
      in the Messages API request.
    - ``build_executors(bot_client, user_client)`` — returns a dict
      ``{tool_name: callable(input_dict) -> json_str}`` the responder
      calls when a matching ``tool_use`` block fires.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from app.logging_setup import get_logger

log = get_logger(__name__)


SLACK_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "slack_search",
        "description": (
            "Полнотекстовый поиск сообщений по всему Slack-workspace "
            "оператора. Использует Slack `search.messages` (user-token). "
            "Возвращает до 20 матчей с каналом, ts, превью и permalink. "
            "Полезно для вопросов вида «что писали X / Y / по теме Z»."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Slack search query. Поддерживает модификаторы "
                        "`from:@user`, `in:#channel`, `after:YYYY-MM-DD`, "
                        "`before:YYYY-MM-DD`."
                    ),
                },
                "count": {
                    "type": "integer",
                    "description": "Сколько результатов вернуть (1-20).",
                    "default": 20,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "slack_get_channel_history",
        "description": (
            "История канала/DM — последние N top-level сообщений (без "
            "thread-replies). Channel = ID вида `Cxxxx`/`Dxxxx`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string"},
                "limit": {"type": "integer", "default": 30},
                "oldest": {
                    "type": "string",
                    "description": "ts low bound (optional).",
                },
            },
            "required": ["channel"],
        },
    },
    {
        "name": "slack_get_thread_replies",
        "description": (
            "Все сообщения в треде, включая parent. `thread_ts` — ts "
            "родительского сообщения треда."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string"},
                "thread_ts": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
            },
            "required": ["channel", "thread_ts"],
        },
    },
    {
        "name": "slack_post_message",
        "description": (
            "Отправить сообщение в канал/DM/тред от имени бота. "
            "thread_ts опционален — если задан, ответ уйдёт в тред."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string"},
                "text": {"type": "string"},
                "thread_ts": {"type": "string"},
            },
            "required": ["channel", "text"],
        },
    },
    {
        "name": "slack_users_info",
        "description": "Профиль Slack-юзера по ID `Uxxxx`.",
        "input_schema": {
            "type": "object",
            "properties": {"user": {"type": "string"}},
            "required": ["user"],
        },
    },
    {
        "name": "slack_users_lookup_by_email",
        "description": "Найти Slack-юзера по email.",
        "input_schema": {
            "type": "object",
            "properties": {"email": {"type": "string"}},
            "required": ["email"],
        },
    },
    {
        "name": "slack_list_channels",
        "description": (
            "Список каналов workspace (public/private/im/mpim) с id, "
            "name, is_private, is_im, is_member."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "types": {
                    "type": "string",
                    "description": (
                        "Comma-separated: public_channel,private_channel,"
                        "im,mpim. Default: только public+private "
                        "(не требует im:read / mpim:read scope)."
                    ),
                    "default": "public_channel,private_channel",
                },
                "limit": {"type": "integer", "default": 200},
            },
        },
    },
    {
        "name": "slack_get_permalink",
        "description": (
            "Permalink на конкретное сообщение по `channel` + `ts`. "
            "Удобно вернуть оператору ссылку «откуда взято»."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string"},
                "ts": {"type": "string"},
            },
            "required": ["channel", "ts"],
        },
    },
]


_MAX_RESULT_CHARS = 8000


def _truncate(s: str, n: int = _MAX_RESULT_CHARS) -> str:
    """Tool_result content shouldn't blow context. Cut hard."""
    if len(s) <= n:
        return s
    return s[:n] + f"\n…[truncated {len(s) - n} chars]"


def _strip_message(m: dict) -> dict:
    """Keep only fields the model actually needs."""
    return {
        "ts": m.get("ts"),
        "user": m.get("user"),
        "thread_ts": m.get("thread_ts"),
        "text": m.get("text"),
        "subtype": m.get("subtype"),
        "reply_count": m.get("reply_count"),
    }


def build_executors(
    *,
    bot_client: Any,
    user_client: Any | None = None,
) -> dict[str, Callable[[dict[str, Any]], str]]:
    """Build name→callable map. Each callable returns a JSON string
    suitable for the ``content`` field of a ``tool_result`` block.

    ``user_client`` is needed only for ``slack_search`` (Slack's
    search.messages rejects bot tokens). If absent, slack_search
    returns a clear error string but everything else works.
    """

    def _ok(payload: Any) -> str:
        return _truncate(json.dumps(payload, ensure_ascii=False, default=str))

    def _err(msg: str) -> str:
        return _ok({"error": msg})

    def slack_search(inp: dict) -> str:
        if user_client is None:
            return _err(
                "search disabled: CEO_BRAIN_SLACK_USER_TOKEN not set"
            )
        try:
            r = user_client.search_messages(
                query=inp["query"],
                count=min(max(int(inp.get("count") or 20), 1), 20),
            )
        except Exception as e:  # noqa: BLE001
            return _err(f"search_messages_failed: {e}")
        if not r.get("ok"):
            return _err(f"search_not_ok: {r.get('error')}")
        matches = (r.get("messages") or {}).get("matches") or []
        return _ok([
            {
                "channel": (m.get("channel") or {}).get("id"),
                "channel_name": (m.get("channel") or {}).get("name"),
                "user": m.get("user"),
                "username": m.get("username"),
                "ts": m.get("ts"),
                "text": m.get("text"),
                "permalink": m.get("permalink"),
            }
            for m in matches
        ])

    def slack_get_channel_history(inp: dict) -> str:
        try:
            r = bot_client.conversations_history(
                channel=inp["channel"],
                limit=min(max(int(inp.get("limit") or 30), 1), 200),
                oldest=inp.get("oldest") or "0",
            )
        except Exception as e:  # noqa: BLE001
            return _err(f"history_failed: {e}")
        if not r.get("ok"):
            return _err(f"history_not_ok: {r.get('error')}")
        return _ok({
            "messages": [_strip_message(m) for m in (r.get("messages") or [])],
            "has_more": r.get("has_more"),
        })

    def slack_get_thread_replies(inp: dict) -> str:
        try:
            r = bot_client.conversations_replies(
                channel=inp["channel"],
                ts=inp["thread_ts"],
                limit=min(max(int(inp.get("limit") or 50), 1), 200),
            )
        except Exception as e:  # noqa: BLE001
            return _err(f"replies_failed: {e}")
        if not r.get("ok"):
            return _err(f"replies_not_ok: {r.get('error')}")
        return _ok({
            "messages": [_strip_message(m) for m in (r.get("messages") or [])],
            "has_more": r.get("has_more"),
        })

    def slack_post_message(inp: dict) -> str:
        kwargs: dict[str, Any] = {
            "channel": inp["channel"],
            "text": inp["text"],
        }
        thread = inp.get("thread_ts")
        if thread:
            kwargs["thread_ts"] = thread
        try:
            r = bot_client.chat_postMessage(**kwargs)
        except Exception as e:  # noqa: BLE001
            return _err(f"post_failed: {e}")
        if not r.get("ok"):
            return _err(f"post_not_ok: {r.get('error')}")
        return _ok({
            "ok": True, "ts": r.get("ts"), "channel": r.get("channel"),
        })

    def slack_users_info(inp: dict) -> str:
        try:
            r = bot_client.users_info(user=inp["user"])
        except Exception as e:  # noqa: BLE001
            return _err(f"users_info_failed: {e}")
        if not r.get("ok"):
            return _err(f"users_info_not_ok: {r.get('error')}")
        u = r.get("user") or {}
        return _ok({
            "id": u.get("id"),
            "name": u.get("name"),
            "real_name": u.get("real_name"),
            "email": (u.get("profile") or {}).get("email"),
            "tz": u.get("tz"),
            "is_bot": u.get("is_bot"),
        })

    def slack_users_lookup_by_email(inp: dict) -> str:
        try:
            r = bot_client.users_lookupByEmail(email=inp["email"])
        except Exception as e:  # noqa: BLE001
            return _err(f"lookup_failed: {e}")
        if not r.get("ok"):
            return _err(f"lookup_not_ok: {r.get('error')}")
        u = r.get("user") or {}
        return _ok({
            "id": u.get("id"),
            "name": u.get("name"),
            "real_name": u.get("real_name"),
        })

    def slack_list_channels(inp: dict) -> str:
        # FR-CB2-3.34 — use `users.conversations` instead of
        # `conversations.list`: the former returns only channels the
        # calling bot is actually a member of. The latter lists ALL
        # workspace channels (with is_member flag), which confuses
        # the model when it tries to answer «в каких каналах ты
        # добавлен» and returns is_member=False entries.
        try:
            r = bot_client.users_conversations(
                types=(
                    inp.get("types")
                    or "public_channel,private_channel"
                ),
                limit=min(max(int(inp.get("limit") or 200), 1), 1000),
                exclude_archived=True,
            )
        except Exception as e:  # noqa: BLE001
            return _err(f"list_failed: {e}")
        if not r.get("ok"):
            return _err(f"list_not_ok: {r.get('error')}")
        return _ok([
            {
                "id": c.get("id"),
                "name": c.get("name") or c.get("user") or "",
                "is_private": c.get("is_private"),
                "is_im": c.get("is_im"),
                "is_member": c.get("is_member"),
            }
            for c in (r.get("channels") or [])
        ])

    def slack_get_permalink(inp: dict) -> str:
        try:
            r = bot_client.chat_getPermalink(
                channel=inp["channel"],
                message_ts=inp["ts"],
            )
        except Exception as e:  # noqa: BLE001
            return _err(f"permalink_failed: {e}")
        if not r.get("ok"):
            return _err(f"permalink_not_ok: {r.get('error')}")
        return _ok({"permalink": r.get("permalink")})

    return {
        "slack_search": slack_search,
        "slack_get_channel_history": slack_get_channel_history,
        "slack_get_thread_replies": slack_get_thread_replies,
        "slack_post_message": slack_post_message,
        "slack_users_info": slack_users_info,
        "slack_users_lookup_by_email": slack_users_lookup_by_email,
        "slack_list_channels": slack_list_channels,
        "slack_get_permalink": slack_get_permalink,
    }


__all__ = [
    "SLACK_TOOL_SCHEMAS",
    "build_executors",
]
