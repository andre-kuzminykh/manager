"""FR-CB2-2.10 / 2.11 — Slack user / channel name LRU cache.

Calls to ``users.info`` and ``conversations.info`` are expensive
relative to the archive write rate. Wrap the lookups in
``functools.lru_cache`` and let the caller pre-register the
underlying fetcher so tests can stub it.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Callable

_user_fetcher: Callable[[str], dict] | None = None
_channel_fetcher: Callable[[str], dict] | None = None


def set_user_fetcher(fn: Callable[[str], dict] | None) -> None:
    global _user_fetcher
    _user_fetcher = fn
    resolve_user_display_name.cache_clear()


def set_channel_fetcher(fn: Callable[[str], dict] | None) -> None:
    global _channel_fetcher
    _channel_fetcher = fn
    resolve_channel_name.cache_clear()


def _fetch_users_info(user_id: str) -> dict:
    """Indirection so tests can monkeypatch the *fetcher* without
    touching the LRU cache itself."""
    if _user_fetcher is None:
        return {}
    return _user_fetcher(user_id) or {}


def _fetch_conversations_info(channel_id: str) -> dict:
    if _channel_fetcher is None:
        return {}
    return _channel_fetcher(channel_id) or {}


@lru_cache(maxsize=5000)
def resolve_user_display_name(user_id: str) -> str | None:
    """FR-CB2-2.10 — return display name for a Slack user id."""
    if not user_id:
        return None
    info = _fetch_users_info(user_id)
    if not info:
        return None
    # `users.info.user` exposes profile.display_name first, then
    # real_name; we accept either shape.
    user = info.get("user") if "user" in info else info
    if not isinstance(user, dict):
        return None
    profile = user.get("profile") or {}
    return (
        profile.get("display_name_normalized")
        or profile.get("display_name")
        or user.get("real_name")
        or user.get("name")
        or None
    )


@lru_cache(maxsize=1000)
def resolve_channel_name(channel_id: str) -> str | None:
    """FR-CB2-2.11 — return channel name for a Slack channel id."""
    if not channel_id:
        return None
    info = _fetch_conversations_info(channel_id)
    if not info:
        return None
    channel = info.get("channel") if "channel" in info else info
    if not isinstance(channel, dict):
        return None
    return channel.get("name") or channel.get("name_normalized") or None


__all__ = [
    "resolve_channel_name",
    "resolve_user_display_name",
    "set_channel_fetcher",
    "set_user_fetcher",
]
