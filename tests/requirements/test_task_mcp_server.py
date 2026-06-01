"""FR-TV-090 — external task MCP server (stdio JSON-RPC). Pure dispatch tests:
no DB, no network. The executors are fakes that return JSON strings (the same
contract the real ones honour)."""
from __future__ import annotations

import io
import json

import pytest

srv = pytest.importorskip("app.ceo_brain.task_mcp_server")

SCHEMAS = [
    {"name": "search_tasks", "description": "d", "input_schema": {"type": "object"}},
    {"name": "update_task_status", "description": "d", "input_schema": {"type": "object"}},
]
EXECUTORS = {
    "search_tasks": lambda inp: json.dumps([{"task_id": 1, "title": "x"}]),
    "update_task_status": lambda inp: json.dumps(
        {"ok": True, "field": "status", "from": "todo", "to": "done",
         "actor": inp.get("actor_id")}),
}


def _disp(req):
    return srv.dispatch(req, executors=EXECUTORS, schemas=SCHEMAS)


def test_initialize_returns_protocol_and_server_info():
    r = _disp({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert r["result"]["protocolVersion"] == srv.PROTOCOL_VERSION
    assert r["result"]["serverInfo"]["name"] == "manager-tasks"
    assert "tools" in r["result"]["capabilities"]


def test_initialized_notification_returns_none():
    assert _disp({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tools_list_maps_schemas_to_mcp():
    r = _disp({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = r["result"]["tools"]
    assert {t["name"] for t in tools} == {"search_tasks", "update_task_status"}
    assert all("inputSchema" in t and "description" in t for t in tools)


def test_tools_call_routes_to_executor_and_wraps_result():
    r = _disp({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
               "params": {"name": "search_tasks", "arguments": {"query": "x"}}})
    content = r["result"]["content"]
    assert content[0]["type"] == "text"
    assert json.loads(content[0]["text"])[0]["task_id"] == 1
    assert r["result"]["isError"] is False


def test_tools_call_passes_arguments_through():
    r = _disp({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
               "params": {"name": "update_task_status",
                          "arguments": {"task_id": 1, "new_status": "done",
                                        "actor_id": "U_BOSS"}}})
    payload = json.loads(r["result"]["content"][0]["text"])
    assert payload["to"] == "done" and payload["actor"] == "U_BOSS"


def test_unknown_tool_is_jsonrpc_error():
    r = _disp({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
               "params": {"name": "nope", "arguments": {}}})
    assert r["error"]["code"] == -32601


def test_unknown_method_request_errors_but_notification_ignored():
    assert _disp({"jsonrpc": "2.0", "id": 6, "method": "weird"})["error"]["code"] == -32601
    assert _disp({"jsonrpc": "2.0", "method": "weird_notif"}) is None


def test_failing_executor_is_reported_as_tool_result_not_crash():
    execs = {"boom": lambda inp: (_ for _ in ()).throw(RuntimeError("kaboom"))}
    r = srv.dispatch(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
         "params": {"name": "boom", "arguments": {}}},
        executors=execs, schemas=[{"name": "boom", "description": "d",
                                   "input_schema": {}}],
    )
    assert "error" in json.loads(r["result"]["content"][0]["text"])


def test_serve_stdio_roundtrip():
    inp = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
        + "\n"  # blank line ignored
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
    )
    out = io.StringIO()
    srv.serve_stdio(executors=EXECUTORS, schemas=SCHEMAS, stdin=inp, stdout=out)
    lines = [l for l in out.getvalue().splitlines() if l.strip()]
    assert len(lines) == 1                      # only tools/list got a response
    assert {t["name"] for t in json.loads(lines[0])["result"]["tools"]} == \
        {"search_tasks", "update_task_status"}
