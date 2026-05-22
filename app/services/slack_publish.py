"""FR-CR-05-194 — reusable Slack publish service для Zoom recordings.

Используется:
  - Pipeline `_step_send_to_slack` (auto-send после короткого summary)
  - CLI `ops/send_one_zoom.py` (manual operator review + push)

Контракт:
  - Принимает уже подготовленный `ZoomRecording` row (с short_summary).
  - Берёт tasks из БД через `_build_todo_section` (FR-CR-05-119 + 163 filter).
  - Postит parent + thread reply в указанный Slack channel.
  - НЕ удаляет tasks из БД (это операторский CLI flag, не pipeline).
  - Idempotent через row.slack_post_ts (если уже postnut, skip).
  - Safe fallback на любую ошибку — log + возврат {ok:False, error}.
"""
from __future__ import annotations

import os
from typing import Any

from app.logging_setup import get_logger

log = get_logger(__name__)


def _is_enabled() -> bool:
    """FR-CR-05-194b — feature flag `AUTO_SEND_TO_SLACK_ENABLED`."""
    return os.environ.get(
        "AUTO_SEND_TO_SLACK_ENABLED", "false",
    ).strip().lower() in ("true", "1", "yes", "on")


def _get_channel() -> str | None:
    """FR-CR-05-194c — channel из env `AUTO_SEND_TO_SLACK_CHANNEL`.
    Empty → disabled."""
    ch = os.environ.get("AUTO_SEND_TO_SLACK_CHANNEL", "").strip()
    return ch or None


def _get_token_key() -> str:
    """FR-CR-05-194d — token-key из env `AUTO_SEND_TO_SLACK_TOKEN_KEY`,
    default 'ceo_brain_slack_bot_token' (CEO Brain DM)."""
    return os.environ.get(
        "AUTO_SEND_TO_SLACK_TOKEN_KEY", "ceo_brain_slack_bot_token",
    ).strip() or "ceo_brain_slack_bot_token"


def publish_zoom_recording_to_slack(
    session,
    row,
    *,
    channel: str,
    token: str,
    use_db_tasks: bool = True,
    no_tasks: bool = False,
) -> dict:
    """FR-CR-05-194a — Slack publish одной Zoom recording.

    Returns:
        {"ok": True, "parent_ts": str, "tasks_posted": bool}
        {"ok": False, "error": str, "step": str}
    """
    if not channel:
        return {"ok": False, "error": "channel empty", "step": "validate"}
    if not token:
        return {"ok": False, "error": "token empty", "step": "validate"}
    if not (row.short_summary or "").strip():
        return {"ok": False, "error": "short_summary empty", "step": "validate"}

    # Idempotent skip — если уже postnut в этот channel
    existing_ts = getattr(row, "slack_post_ts", None)
    if existing_ts:
        log.info("slack_publish_skipped_already_posted",
                 zoom_id=getattr(row, "zoom_id", None),
                 slack_post_ts=existing_ts)
        return {"ok": True, "parent_ts": existing_ts, "tasks_posted": False,
                "skipped_reason": "already_posted"}

    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except ImportError as e:
        return {"ok": False, "error": f"slack_sdk import: {e}",
                "step": "import"}

    from ops.send_one_zoom import _split_short_summary
    from ops._send_helpers import (
        SLACK_TEXT_CHUNK_CHARS, _compact_for_slack,
        _split_for_slack, _to_slack_mrkdwn, build_parent_raw,
    )
    from app.fireflies.pipeline import _build_todo_section
    from app.models import TaskSourceKind

    body, reused_todo = _split_short_summary(row.short_summary)

    # Tasks block
    tasks_text = ""
    if not no_tasks:
        if use_db_tasks:
            tasks_text = _build_todo_section(
                session,
                source_kind=TaskSourceKind.zoom,
                source_conversation_id=row.zoom_id,
            )
        elif reused_todo:
            tasks_text = reused_todo
    if tasks_text:
        tasks_text = _compact_for_slack(_to_slack_mrkdwn(tasks_text))

    parent_raw = build_parent_raw(body, tasks_text)
    parent_text = _compact_for_slack(_to_slack_mrkdwn(parent_raw))
    chunks = _split_for_slack(parent_text, limit=SLACK_TEXT_CHUNK_CHARS)

    log.info("slack_publish_starting",
             zoom_id=getattr(row, "zoom_id", None),
             channel=channel, chunks=len(chunks),
             has_tasks=bool(tasks_text))

    client = WebClient(token=token)
    try:
        resp = client.chat_postMessage(
            channel=channel, text=chunks[0],
            unfurl_links=False, unfurl_media=False,
        )
        parent_ts = (resp.data or {}).get("ts")
        for c in chunks[1:]:
            client.chat_postMessage(
                channel=channel, text=c, thread_ts=parent_ts,
                unfurl_links=False, unfurl_media=False,
            )
        tasks_posted = False
        if tasks_text:
            client.chat_postMessage(
                channel=channel, text=tasks_text,
                thread_ts=parent_ts,
                unfurl_links=False, unfurl_media=False,
            )
            tasks_posted = True
    except SlackApiError as e:
        err = (
            e.response.data.get("error")
            if e.response is not None
            and isinstance(e.response.data, dict)
            else str(e)
        )
        log.warning("slack_publish_api_error",
                    zoom_id=getattr(row, "zoom_id", None), error=err)
        return {"ok": False, "error": err, "step": "post"}

    # Persist post_ts для idempotency
    if hasattr(row, "slack_post_ts"):
        row.slack_post_ts = parent_ts
        session.flush()

    log.info("slack_publish_done",
             zoom_id=getattr(row, "zoom_id", None),
             parent_ts=parent_ts, tasks_posted=tasks_posted)
    return {"ok": True, "parent_ts": parent_ts,
            "tasks_posted": tasks_posted}


def maybe_auto_publish(session, row, *, settings) -> dict | None:
    """FR-CR-05-194e — pipeline auto-send helper.

    Зовётся из `_step_send_to_slack`. Проверяет env-flags,
    выбирает token, channel — и зовёт publish_zoom_recording_to_slack.

    Returns None если auto-publish disabled (по env), иначе dict
    с результатом publish.
    """
    if not _is_enabled():
        return None
    channel = _get_channel()
    if not channel:
        return None
    token_key = _get_token_key()
    token = getattr(settings, token_key, "") or ""
    if not token:
        log.warning("slack_publish_auto_no_token", token_key=token_key)
        return None
    return publish_zoom_recording_to_slack(
        session, row, channel=channel, token=token,
        use_db_tasks=True, no_tasks=False,
    )
