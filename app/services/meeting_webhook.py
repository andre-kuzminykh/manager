"""FR-CR-05-160 — POST a meeting summary to an external webhook
(default consumer: n8n at thehumanoid.app.n8n.cloud). Same payload
shape for both Zoom and Fireflies recordings — operator-pinned:
«вывод коллеге через webhook».

The webhook call runs AFTER the Slack mirror, in the same try/except
guard, so a webhook failure can't break the rest of the pipeline.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

from app.logging_setup import get_logger

log = get_logger(__name__)


def post_meeting_to_webhook(
    *,
    webhook_url: str,
    source: str,
    source_id: str,
    title: str | None,
    meeting_date: datetime | None,
    duration_seconds: int | None,
    short_summary: str | None,
    detailed_summary: str | None,
    google_doc_url: str | None,
    participants: list[str] | None = None,
    tasks_count: int | None = None,
    timeout: float = 10.0,
) -> bool:
    """POST the meeting JSON to `webhook_url`. Returns True on 2xx,
    False on any failure (logged, never raises).

    Empty webhook_url → no-op, returns False silently."""
    if not webhook_url:
        return False
    if not (short_summary or "").strip():
        # Don't push empty summaries to the webhook — keeps the
        # consumer side clean (mirrors slack_mirror gating).
        return False

    payload: dict[str, Any] = {
        "source": source,
        "source_id": source_id,
        "title": title or "",
        "meeting_date": (
            meeting_date.isoformat() if meeting_date else None
        ),
        "duration_seconds": duration_seconds,
        "short_summary": short_summary,
        "detailed_summary": detailed_summary,
        "google_doc_url": google_doc_url,
        "participants": participants or [],
        "tasks_count": tasks_count,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        webhook_url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            ok = 200 <= int(status) < 300
            log.info(
                "meeting_webhook_posted",
                source=source, source_id=source_id,
                status=status, ok=ok,
                payload_chars=len(body),
            )
            return ok
    except urllib.error.HTTPError as e:
        log.warning(
            "meeting_webhook_http_error",
            source=source, source_id=source_id,
            status=e.code,
        )
        return False
    except Exception as e:  # noqa: BLE001
        log.warning(
            "meeting_webhook_unexpected_error",
            source=source, source_id=source_id, error=str(e),
        )
        return False


__all__ = ["post_meeting_to_webhook"]
