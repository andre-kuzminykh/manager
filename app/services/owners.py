"""Resolve a free-text owner hint against the allowed-owners registry."""
from __future__ import annotations

import re


_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")


def resolve_owner_hint(
    *,
    hint_text: str | None,
    allowed_owners: list[dict[str, str]],
) -> dict[str, str] | None:
    """Best-effort mapping of a natural-language hint to an allowed owner.

    Returns a dict {slack_user_id, display_name} or None if no match is
    found. Never returns an owner outside of the allowed list.
    """

    if not hint_text or not allowed_owners:
        return None

    by_id = {o["slack_user_id"]: o for o in allowed_owners}
    by_name_lower = {o["display_name"].lower(): o for o in allowed_owners}

    # 1) Slack mention tokens <@UXXX> always win.
    for sid in _MENTION_RE.findall(hint_text):
        if sid in by_id:
            return by_id[sid]

    lower = hint_text.lower().strip()
    # 2) Exact display-name match.
    if lower in by_name_lower:
        return by_name_lower[lower]

    # 3) Substring match: pick the *longest* allowed name that appears in the
    #    hint (longest to avoid matching "Ivan" inside "Ivanov Sr." first).
    candidates = sorted(allowed_owners, key=lambda o: -len(o["display_name"]))
    for o in candidates:
        name = o["display_name"].lower()
        if name and name in lower:
            return o

    return None
