"""FR-CR-05-128 — per-recording trace log.

Operator-pinned: «мне под каждый вызов надо в трейсы складывать
с датой и временем». Every meeting-pipeline event (step
boundaries, LLM round-trips, Whisper calls, Telegram sends)
appends one JSONL line to `/app/traces/<source>-<recording-id>.jsonl`
with a UTC timestamp + event name + structured fields.

The directory is intended to be volume-mounted to the host so
the operator can `cat traces/zoom-XYZ.jsonl | jq` after a run
to walk through every API call. Falls back to a no-op when
`MEETING_TRACE_DIR` is unset / unwritable.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any

from app.logging_setup import get_logger

log = get_logger(__name__)


_TRACE_DIR_ENV = "MEETING_TRACE_DIR"
_DEFAULT_TRACE_DIR = "/app/traces"
_FILE_LOCK = threading.Lock()
_DIR_CHECKED = False


def _trace_dir() -> str | None:
    """Resolve the trace dir, creating it once on first use.
    Returns None when the dir can't be created so callers can
    silently no-op (the structlog log line still fires)."""
    global _DIR_CHECKED  # noqa: PLW0603
    path = os.environ.get(_TRACE_DIR_ENV) or _DEFAULT_TRACE_DIR
    if not _DIR_CHECKED:
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as e:
            log.info("trace_dir_unavailable", path=path, error=str(e))
            _DIR_CHECKED = True
            return None
        _DIR_CHECKED = True
    return path


def _safe(s: str) -> str:
    """Sanitise a recording-id for filesystem use. Zoom UUIDs
    contain `/` and `=` which create unwanted subdirs."""
    return (s or "unknown").replace("/", "_").replace("=", "")


def _coerce(v: Any) -> Any:
    """JSON-safe projection. Datetimes → isoformat, sets/tuples
    → lists, anything else with `.value` (Enum) → its value,
    else `repr` for un-serialisable objects."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).isoformat()
    if isinstance(v, (set, tuple)):
        return [_coerce(x) for x in v]
    if isinstance(v, list):
        return [_coerce(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _coerce(x) for k, x in v.items()}
    if hasattr(v, "value") and not callable(v.value):
        return _coerce(v.value)
    return repr(v)


def trace_event(
    *,
    source: str,
    recording_id: str | None,
    event: str,
    **fields: Any,
) -> None:
    """Append one JSONL line for `(<source>, <recording-id>)`.

    `source` is `"fireflies"` / `"zoom"` (becomes the file
    prefix); `recording_id` is `MeetingRecording.fireflies_id`
    / `ZoomRecording.zoom_id`. `event` is the structured event
    name (mirror the structlog event when there is one). All
    extra kwargs land under the `fields` block.

    Failures swallowed — tracing must NEVER break the pipeline.
    """
    path = _trace_dir()
    if path is None or not recording_id:
        return
    fname = f"{source}-{_safe(str(recording_id))}.jsonl"
    full = os.path.join(path, fname)
    line = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "recording_id": recording_id,
        "event": event,
        "fields": _coerce(fields),
    }
    try:
        encoded = json.dumps(line, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as e:
        log.info("trace_event_serialisation_failed", event=event, error=str(e))
        return
    try:
        with _FILE_LOCK:
            with open(full, "a", encoding="utf-8") as f:
                f.write(encoded + "\n")
    except OSError as e:
        log.info("trace_event_write_failed", path=full, error=str(e))


__all__ = ["trace_event"]
