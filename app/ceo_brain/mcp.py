"""FR-CB2-4.x — MCP server registry for the Claude responder.

Anthropic Messages API accepts an ``mcp_servers=[…]`` list. We
sources it from the ``MCP_SERVERS`` env JSON (list of dicts with
``name`` / ``url`` / ``type`` / optional ``auth``) and let
individual ``MCP_<NAME>_OAUTH_TOKEN`` env vars override per-server
tokens for secrets-manager hosted deployments.
"""
from __future__ import annotations

import os
import re
from typing import Any, Callable

from app.ceo_brain.config import get_mcp_servers
from app.logging_setup import get_logger

log = get_logger(__name__)


_TOKEN_VAR_SAFE = re.compile(r"[^A-Z0-9_]+")


def load_mcp_servers() -> list[dict[str, Any]]:
    """FR-CB2-4.1 — parse + validate the ``MCP_SERVERS`` env JSON.
    Returns empty list when unset / malformed (logged, not raised)."""
    servers = get_mcp_servers()
    log.info(
        "ceo_brain_mcp_loaded",
        count=len(servers),
        names=[s.get("name") for s in servers],
    )
    return servers


def resolve_oauth_token(server_name: str) -> str | None:
    """FR-CB2-4.3 — return OAuth token for a server. Prefer
    explicit per-server env var ``MCP_<NAME>_OAUTH_TOKEN`` (so it
    can be sourced from Secret Manager); fall back to the value
    embedded in the MCP_SERVERS JSON entry."""
    if not server_name:
        return None
    key = "MCP_" + _TOKEN_VAR_SAFE.sub("_", server_name.upper()) + "_OAUTH_TOKEN"
    token = os.environ.get(key)
    if token:
        return token
    for s in get_mcp_servers():
        if s.get("name") == server_name:
            auth = s.get("authorization_token") or s.get("auth")
            return auth if isinstance(auth, str) and auth else None
    return None


def filter_reachable_servers(
    servers: list[dict[str, Any]],
    probe: Callable[[str], bool],
) -> list[dict[str, Any]]:
    """FR-CB2-4.4 — drop servers that fail the ``probe(url)``
    health-check. Anthropic still sees the remaining ones and tool
    failures are signalled as ``is_error=true`` on the individual
    tool_use response, so the rest of the flow keeps running."""
    out: list[dict[str, Any]] = []
    for s in servers:
        url = s.get("url") or ""
        try:
            ok = bool(probe(url))
        except Exception as e:  # noqa: BLE001
            log.warning(
                "ceo_brain_mcp_probe_error",
                name=s.get("name"), error=str(e),
            )
            ok = False
        if ok:
            out.append(s)
        else:
            log.info("ceo_brain_mcp_unreachable", name=s.get("name"))
    return out


__all__ = [
    "filter_reachable_servers",
    "load_mcp_servers",
    "resolve_oauth_token",
]
