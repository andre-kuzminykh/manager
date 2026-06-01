"""FR-TV-090 — minimal stdio MCP server exposing the task tools to an EXTERNAL
Claude (or any MCP client). Newline-delimited JSON-RPC 2.0 over stdio; no extra
dependency. Reuses `TASK_TOOL_SCHEMAS` + `build_task_executors` (single source
of truth shared with the in-process CEO-brain).

SAFETY: gated by `TASK_MCP_SERVER_ENABLED`. Writes still require
`actor_id ∈ CEO_BRAIN_ALLOWED_USERS` (enforced inside the executors), so the
allow-list is the real write-gate regardless of transport. For an HTTP/SSE
transport a bearer token would be added; the stdio transport relies on the
process launch + the allow-list.

Entry point: `python -m ops.task_mcp_server`.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Callable

from app.logging_setup import get_logger

log = get_logger(__name__)

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "manager-tasks", "version": "0.1.0"}


def mcp_tools(schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map our Anthropic-style schemas to MCP tool descriptors."""
    return [
        {"name": s["name"], "description": s["description"],
         "inputSchema": s["input_schema"]}
        for s in schemas
    ]


def dispatch(
    request: dict[str, Any],
    *,
    executors: dict[str, Callable[[dict[str, Any]], str]],
    schemas: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Pure JSON-RPC dispatch → response dict, or None for notifications.
    Never raises (a failing tool is reported as a tool result, not a crash)."""
    method = request.get("method")
    rid = request.get("id")
    params = request.get("params") or {}

    def _result(r: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": rid, "result": r}

    def _error(code: int, msg: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}

    if method == "initialize":
        return _result({
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": SERVER_INFO,
            "capabilities": {"tools": {}},
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return _result({})
    if method == "tools/list":
        return _result({"tools": mcp_tools(schemas)})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = executors.get(name)
        if fn is None:
            return _error(-32601, f"unknown tool: {name}")
        try:
            text = fn(args)  # executors return a json string and never raise
        except Exception as e:  # noqa: BLE001 — defensive
            text = json.dumps({"error": str(e)}, ensure_ascii=False)
        log.info("task_mcp_tools_call", tool=name)
        return _result({"content": [{"type": "text", "text": text}],
                        "isError": False})
    # Unknown method: respond with error for requests, ignore notifications.
    if rid is None:
        return None
    return _error(-32601, f"unknown method: {method}")


def serve_stdio(
    *,
    executors: dict[str, Callable[[dict[str, Any]], str]],
    schemas: list[dict[str, Any]],
    stdin: Any = None,
    stdout: Any = None,
) -> None:
    """Blocking newline-delimited JSON-RPC loop over stdio."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    log.info("task_mcp_server_started", tools=[s["name"] for s in schemas])
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = dispatch(req, executors=executors, schemas=schemas)
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False, default=str) + "\n")
            stdout.flush()


__all__ = ["dispatch", "serve_stdio", "mcp_tools", "PROTOCOL_VERSION", "SERVER_INFO"]
