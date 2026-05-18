"""FR-CB2-5.x — CEO Brain Bot env-driven configuration.

Thin wrappers around ``Settings`` so individual call-sites don't
have to know which env var holds what. Plus the conversion of a
few raw env strings into structured types (channel whitelist,
MCP servers JSON, archive directory path).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from app.config import Settings, get_settings


def get_archive_dir(settings: Settings | None = None) -> Path:
    """FR-CB2-5.4 — return the JSONL archive directory as a Path.
    Honours the ``CEO_BRAIN_ARCHIVE_DIR`` env override; falls back
    to the Settings default."""
    raw = (
        os.environ.get("CEO_BRAIN_ARCHIVE_DIR")
        or (settings or get_settings()).ceo_brain_archive_dir
    )
    return Path(raw)


def get_slack_tokens(
    settings: Settings | None = None,
) -> tuple[str, str]:
    """FR-CB2-5.5 — return ``(app_token, bot_token)`` for the CEO
    Brain Slack app, distinct from any other bot tokens in the
    project."""
    s = settings or get_settings()
    return (
        os.environ.get("CEO_BRAIN_SLACK_APP_TOKEN")
        or s.ceo_brain_slack_app_token,
        os.environ.get("CEO_BRAIN_SLACK_BOT_TOKEN")
        or s.ceo_brain_slack_bot_token,
    )


def get_archive_channel_whitelist(
    settings: Settings | None = None,
) -> set[str]:
    """FR-CB2-5.6 — comma-separated list of channel IDs to
    archive. Empty = archive everything we hear."""
    raw = (
        os.environ.get("CEO_BRAIN_ARCHIVE_CHANNELS")
        or (settings or get_settings()).ceo_brain_archive_channels
        or ""
    )
    return {p.strip() for p in raw.split(",") if p.strip()}


def get_mcp_servers_raw(
    settings: Settings | None = None,
) -> str:
    return (
        os.environ.get("MCP_SERVERS")
        or (settings or get_settings()).ceo_brain_mcp_servers
        or ""
    )


def get_mcp_servers(settings: Settings | None = None) -> list[dict]:
    """FR-CB2-4.1 — parse the ``MCP_SERVERS`` env JSON. Returns
    an empty list when unset / unparseable rather than raising,
    so the responder still starts in degraded mode."""
    raw = get_mcp_servers_raw(settings)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        url = (item.get("url") or "").strip()
        kind = (item.get("type") or "sse").strip().lower()
        if not name or not url:
            continue
        if kind not in {"sse", "http"}:
            continue
        normalised = {"name": name, "url": url, "type": kind}
        auth = item.get("auth")
        if auth:
            normalised["auth"] = auth
        out.append(normalised)
    return out


__all__ = [
    "get_archive_channel_whitelist",
    "get_archive_dir",
    "get_mcp_servers",
    "get_mcp_servers_raw",
    "get_slack_tokens",
]
