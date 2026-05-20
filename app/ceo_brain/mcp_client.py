"""FR-CB2-3.31 — Direct HTTP client for MCP servers.

Bypasses Anthropic's MCP connector entirely. Talks JSON-RPC over
HTTP to n8n MCP endpoints. Diagnostic 2026-05-20 proved that:
  * Direct HTTP `tools/call` to n8n returns 60K-char transcript
    in 1.97 sec.
  * The same query through Anthropic-MCP with multi-step reasoning
    takes 60-300+ sec or times out.

Protocol per spec (streamable HTTP transport, version 2025-03-26):
  1. POST `initialize` → server returns 200 + `Mcp-Session-Id` header
  2. POST `notifications/initialized` (no response expected)
  3. POST `tools/call` with the session id → returns SSE-style
     `event: message\ndata: {...}` body OR plain JSON.

n8n MCP endpoints accept both `application/json` and
`text/event-stream` accept headers; we send both for compatibility.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any

import requests

from app.logging_setup import get_logger

log = get_logger(__name__)


_DEFAULT_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
_PROTOCOL_VERSION = "2025-03-26"
_CLIENT_INFO = {"name": "ceo-brain-direct-http", "version": "1.0"}


@dataclass
class MCPSession:
    """Lightweight cached session for one MCP endpoint."""
    url: str
    session_id: str
    http: requests.Session = field(default_factory=requests.Session)
    initialized: bool = False


# Module-level cache so we don't re-init per question. Sessions
# stay valid as long as the n8n server holds them.
_session_cache: dict[str, MCPSession] = {}
_session_lock = threading.Lock()


def _parse_sse_or_json(body: str) -> dict | None:
    """n8n returns either plain JSON or SSE-formatted `event:
    message\\ndata: {...}\\n\\n`. Handle both."""
    if not body:
        return None
    body = body.strip()
    if body.startswith("{"):
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None
    # SSE: extract the last `data: ...` block (usually only one).
    for match in re.finditer(r"^data:\s*(.+)$", body, flags=re.MULTILINE):
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
    return None


def _init_session(url: str, *, timeout: float = 15.0) -> MCPSession:
    """Open a fresh MCP session: `initialize` + notification.
    Caches by URL. Lock-protected for thread safety."""
    with _session_lock:
        cached = _session_cache.get(url)
        if cached and cached.initialized:
            return cached
        http = requests.Session()
        resp = http.post(
            url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": _CLIENT_INFO,
                },
            },
            headers=_DEFAULT_HEADERS,
            timeout=timeout,
        )
        resp.raise_for_status()
        session_id = resp.headers.get("Mcp-Session-Id") or ""
        if not session_id:
            raise RuntimeError(
                f"MCP init returned no Mcp-Session-Id (url={url})"
            )
        # Send `notifications/initialized` — completes handshake.
        # No response expected (notification = no `id`).
        try:
            http.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                },
                headers={**_DEFAULT_HEADERS, "Mcp-Session-Id": session_id},
                timeout=timeout,
            )
        except Exception as e:  # noqa: BLE001
            log.info(
                "ceo_brain_mcp_init_notify_failed",
                url=url, error=str(e),
            )
        sess = MCPSession(
            url=url, session_id=session_id, http=http, initialized=True,
        )
        _session_cache[url] = sess
        log.info("ceo_brain_mcp_session_initialized", url=url[:60])
        return sess


def list_tools(url: str, *, timeout: float = 15.0) -> list[dict]:
    """Fetch the tool catalogue from an MCP endpoint. Returns a
    list of ``{name, description, inputSchema}`` dicts. Empty
    list on failure."""
    try:
        sess = _init_session(url, timeout=timeout)
        resp = sess.http.post(
            url,
            json={
                "jsonrpc": "2.0",
                "id": 100,
                "method": "tools/list",
            },
            headers={
                **_DEFAULT_HEADERS,
                "Mcp-Session-Id": sess.session_id,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        parsed = _parse_sse_or_json(resp.text)
        if not parsed:
            return []
        result = (parsed.get("result") or {}) if isinstance(parsed, dict) else {}
        return result.get("tools") or []
    except Exception as e:  # noqa: BLE001
        log.warning(
            "ceo_brain_mcp_list_tools_failed",
            url=url[:60], error=str(e),
        )
        return []


def _coerce_args_to_schema(
    arguments: dict[str, Any],
    input_schema: dict[str, Any] | None,
) -> dict[str, Any]:
    """FR-CB2-3.31 hotfix — n8n MCP tools often declare every arg as
    JSON Schema `type:"string"`, but planner returns ints/bools.
    n8n then 400s with schema-validation error. Coerce types based
    on the declared schema; if schema is missing, default to string.

    Also: fill any REQUIRED arg that the planner omitted with an
    empty string (n8n typically treats `""` as "no filter"). Without
    this, n8n returns «Required → at <field>» schema errors and
    we lose data even though search criteria were sensible.
    """
    if not isinstance(arguments, dict):
        arguments = {}
    if not isinstance(input_schema, dict):
        return {k: v for k, v in arguments.items()}
    props = input_schema.get("properties") or {}
    required = input_schema.get("required") or []
    out: dict[str, Any] = {}
    def _coerce_one(value: Any, expected: str) -> Any:
        if expected == "string" and not isinstance(value, str):
            return str(value)
        if expected in {"integer", "number"} and not isinstance(value, (int, float)):
            try:
                num = int(str(value))
                return num
            except (TypeError, ValueError):
                try:
                    return float(str(value))
                except (TypeError, ValueError):
                    return value
        if expected == "boolean" and not isinstance(value, bool):
            if isinstance(value, str):
                return value.lower() in {"true", "1", "yes"}
            return bool(value)
        return value

    def _empty_for(expected: str) -> Any:
        if expected == "string":
            return ""
        if expected in {"integer", "number"}:
            return 10
        if expected == "boolean":
            return False
        if expected == "array":
            return []
        if expected == "object":
            return {}
        return ""

    for k, v in arguments.items():
        if v is None:
            continue
        prop = props.get(k) or {}
        expected = (prop.get("type") if isinstance(prop, dict) else None) or "string"
        out[k] = _coerce_one(v, expected)
    # Fill missing required args with type-appropriate defaults.
    for req_key in required:
        if req_key in out:
            continue
        prop = props.get(req_key) or {}
        expected = (prop.get("type") if isinstance(prop, dict) else None) or "string"
        out[req_key] = _empty_for(expected)
    return out


def call_tool(
    *,
    url: str,
    tool_name: str,
    arguments: dict[str, Any],
    timeout: float = 30.0,
    retries: int = 1,
    input_schema: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Call ``tools/call`` on an MCP endpoint. Returns
    ``(ok, content_text)``. Content is the concatenated text of
    every `content` item in the JSON-RPC result (n8n typically
    returns one text block with a JSON-encoded payload inside).

    `retries` — re-init session + retry once on session-expired or
    network glitch. Default 1 → at most 2 attempts total.
    """
    last_error: str = ""
    coerced_args = _coerce_args_to_schema(arguments or {}, input_schema)
    for attempt in range(retries + 1):
        try:
            sess = _init_session(url, timeout=timeout)
            resp = sess.http.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": 200 + attempt,
                    "method": "tools/call",
                    "params": {
                        "name": tool_name,
                        "arguments": coerced_args,
                    },
                },
                headers={
                    **_DEFAULT_HEADERS,
                    "Mcp-Session-Id": sess.session_id,
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            parsed = _parse_sse_or_json(resp.text)
            if not parsed:
                last_error = "empty/unparseable response"
                # Drop the cached session — server may be confused.
                _drop_session(url)
                continue
            if "error" in parsed:
                err = parsed["error"]
                msg = (
                    err.get("message")
                    if isinstance(err, dict) else str(err)
                ) or "unknown jsonrpc error"
                last_error = f"jsonrpc_error: {msg}"
                # Session expired? Re-init.
                if "session" in msg.lower() or "not initialized" in msg.lower():
                    _drop_session(url)
                    continue
                log.warning(
                    "ceo_brain_mcp_tool_jsonrpc_error",
                    url=url[:60], tool=tool_name, error=msg,
                )
                return False, last_error
            result = parsed.get("result") or {}
            content_items = result.get("content") or []
            texts: list[str] = []
            for item in content_items:
                if isinstance(item, dict) and item.get("type") == "text":
                    txt = item.get("text") or ""
                    if txt:
                        texts.append(str(txt))
            joined = "\n\n".join(texts).strip()
            log.info(
                "ceo_brain_mcp_tool_ok",
                url=url[:60], tool=tool_name,
                chars=len(joined), attempt=attempt + 1,
                preview=joined[:300].replace("\n", " "),
            )
            return True, joined
        except Exception as e:  # noqa: BLE001
            last_error = f"{type(e).__name__}: {str(e)[:150]}"
            log.info(
                "ceo_brain_mcp_tool_attempt_failed",
                url=url[:60], tool=tool_name,
                attempt=attempt + 1, error=last_error,
            )
            _drop_session(url)
            continue
    log.warning(
        "ceo_brain_mcp_tool_failed",
        url=url[:60], tool=tool_name, error=last_error,
    )
    return False, last_error


def _drop_session(url: str) -> None:
    """Invalidate a cached session — used on errors so the next
    call re-initialises."""
    with _session_lock:
        _session_cache.pop(url, None)


def reset_all_sessions_for_tests() -> None:
    """Test hook — clear the module-level session cache."""
    with _session_lock:
        _session_cache.clear()


__all__ = [
    "MCPSession",
    "call_tool",
    "list_tools",
    "reset_all_sessions_for_tests",
]
