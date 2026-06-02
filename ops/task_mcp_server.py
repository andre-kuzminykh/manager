"""FR-TV-090 — entry point for the external task MCP server (stdio).

Exposes search_tasks / get_task / resolve_person / update_task_status /
update_task_due / update_task_owner to an external MCP client (e.g. Claude).
Gated by TASK_MCP_SERVER_ENABLED; writes require an allow-listed actor_id.

Run (MCP client config launches this as a subprocess):
    python -m ops.task_mcp_server
"""
from __future__ import annotations

import sys

from app.ceo_brain.task_mcp_server import serve_stdio
from app.ceo_brain.task_tools import build_task_executors, task_tool_schemas
from app.config import get_settings
from app.db import get_session_factory
from app.logging_setup import get_logger, setup_logging

log = get_logger(__name__)


def main() -> int:
    setup_logging()
    s = get_settings()
    if not getattr(s, "task_mcp_server_enabled", False):
        print("TASK_MCP_SERVER_ENABLED is off — refusing to start", file=sys.stderr)
        return 2
    # Same read-only-first gate as the in-process responder: writers are
    # exposed only when TASK_VECTOR_WRITES_ENABLED is set (FR-TV).
    writes = getattr(s, "task_vector_writes_enabled", False)
    executors = build_task_executors(
        session_factory=get_session_factory(), settings=s,
        writes_enabled=writes,
    )
    serve_stdio(executors=executors, schemas=task_tool_schemas(writes_enabled=writes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
