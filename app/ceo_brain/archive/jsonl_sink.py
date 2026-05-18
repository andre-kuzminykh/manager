"""FR-CB2-2.1 — JSONL append-only archive.

One file per (channel, day). New line per event. File mode 0640
(operator + group readable, world denied). When the parent
directory doesn't exist we create it with mode 0750 too.

Layout::

    <archive_dir>/<channel_label>/<YYYY-MM-DD>.jsonl

Where ``<channel_label>`` is the human channel name when known
(``"board"``) else the raw Slack id (``"C123"``).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PENDING_FILENAME = "slack_archive_pending.jsonl"

_FILE_MODE = 0o640
_DIR_MODE = 0o750

_LABEL_SAFE_RE = re.compile(r"[^a-zA-Z0-9_\-.]+")


def _label(channel_id: str, channel_name: str | None) -> str:
    """Pick a safe directory name for the channel — sanitised."""
    raw = (channel_name or channel_id or "unknown").strip() or channel_id
    label = _LABEL_SAFE_RE.sub("-", raw)
    return label.strip("-") or channel_id


def _day_path(
    archive_dir: Path, channel_id: str,
    channel_name: str | None, day: str,
) -> Path:
    return Path(archive_dir) / _label(channel_id, channel_name) / f"{day}.jsonl"


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def write(
    *,
    archive_dir: Path | str,
    channel_id: str,
    channel_name: str | None = None,
    ts: str,
    payload: dict[str, Any],
    day: str | None = None,
) -> Path:
    """FR-CB2-2.1 — append a single JSON line. Returns the target
    file path."""
    archive_dir = Path(archive_dir)
    day = day or _today_str()
    target = _day_path(archive_dir, channel_id, channel_name, day)
    target.parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
    # Fresh file → enforce 0640; existing file gets mode-set on
    # every write so it stays correct under various umasks.
    record = {"ts": ts, "channel_id": channel_id, **payload}
    line = json.dumps(record, ensure_ascii=False)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    try:
        os.chmod(target, _FILE_MODE)
        os.chmod(target.parent, _DIR_MODE)
    except OSError:
        # On non-POSIX (Windows test env) chmod might fail; archive
        # still works, perms enforcement is best-effort there.
        pass
    return target


def write_pending(
    *,
    archive_dir: Path | str,
    record: dict[str, Any],
) -> Path:
    """FR-CB2-2.6 — append a pending record at the archive root.
    Cron job picks these up later for PG-replay."""
    archive_dir = Path(archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
    pending = archive_dir / PENDING_FILENAME
    with pending.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    try:
        os.chmod(pending, _FILE_MODE)
    except OSError:
        pass
    return pending


__all__ = ["PENDING_FILENAME", "write", "write_pending"]
