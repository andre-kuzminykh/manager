"""FR-CB2-2.x — Slack message archive (JSONL + PG mirror)."""
from __future__ import annotations

import os

from app.ceo_brain.archive.jsonl_sink import (
    PENDING_FILENAME,
    write as _jsonl_write,
)
from app.ceo_brain.archive.pg_sink import (
    apply_delete,
    apply_edit,
    write as _pg_write,
)
from app.ceo_brain.archive.service import write_archive
from app.ceo_brain.config import get_archive_channel_whitelist


def should_archive_channel(channel_id: str) -> bool:
    """FR-CB2-5.6 — empty whitelist = archive everything; non-empty
    = only listed channels."""
    if not channel_id:
        return False
    whitelist = get_archive_channel_whitelist()
    if not whitelist:
        return True
    return channel_id in whitelist


# expose the two underlying sinks as submodule attributes so tests
# can ``from app.ceo_brain.archive import jsonl_sink, pg_sink``.
from app.ceo_brain.archive import jsonl_sink, pg_sink  # noqa: E402,F401

__all__ = [
    "PENDING_FILENAME",
    "apply_delete",
    "apply_edit",
    "jsonl_sink",
    "pg_sink",
    "should_archive_channel",
    "write_archive",
]
