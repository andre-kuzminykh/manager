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
    Brain Slack app.

    Resolution order:

      1. ``CEO_BRAIN_SLACK_APP_TOKEN`` / ``CEO_BRAIN_SLACK_BOT_TOKEN``
         — explicit override when you DO want a dedicated app.
      2. Top-level ``SLACK_APP_TOKEN`` / ``SLACK_BOT_TOKEN`` —
         operator-pinned 2026-05-18: reuse the existing
         workspace bot rather than provisioning a new Slack app.

    Empty strings are treated as unset at each level.
    """
    s = settings or get_settings()
    app_token = (
        os.environ.get("CEO_BRAIN_SLACK_APP_TOKEN")
        or s.ceo_brain_slack_app_token
        or os.environ.get("SLACK_APP_TOKEN")
        or s.slack_app_token
        or ""
    )
    bot_token = (
        os.environ.get("CEO_BRAIN_SLACK_BOT_TOKEN")
        or s.ceo_brain_slack_bot_token
        or os.environ.get("SLACK_BOT_TOKEN")
        or s.slack_bot_token
        or ""
    )
    return app_token, bot_token


def get_signing_secret(
    settings: Settings | None = None,
) -> str:
    """HTTP Events HMAC secret. Same fallback ladder as
    `get_slack_tokens` — CEO-Brain override → top-level
    SLACK_SIGNING_SECRET → empty."""
    s = settings or get_settings()
    return (
        os.environ.get("CEO_BRAIN_SIGNING_SECRET")
        or s.ceo_brain_signing_secret
        or os.environ.get("SLACK_SIGNING_SECRET")
        or s.slack_signing_secret
        or ""
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
    "get_signing_secret",
    "get_slack_tokens",
]
