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
    source_kind: Any = None,
    source_conversation_id: str | None = None,
    all_tasks_in_thread: bool = False,
    override_short_summary: str | None = None,
    override_thread_todo_text: str | None = None,
    skip_db_write: bool = False,
) -> dict:
    """FR-CR-05-194a — Slack publish одной recording (Zoom OR Fireflies).

    Args:
        source_kind: TaskSourceKind override (default — auto-detect by row).
            Если row имеет zoom_id — TaskSourceKind.zoom, иначе fireflies.
        source_conversation_id: ID для tasks lookup (default — auto: zoom_id
            или fireflies_id у row).

    Returns:
        {"ok": True, "parent_ts": str, "tasks_posted": bool}
        {"ok": False, "error": str, "step": str}
    """
    if not channel:
        return {"ok": False, "error": "channel empty", "step": "validate"}
    if not token:
        return {"ok": False, "error": "token empty", "step": "validate"}
    # short_summary может прийти через override (in-memory V2 без DB write)
    short_summary_text = override_short_summary or row.short_summary or ""
    if not short_summary_text.strip():
        return {"ok": False, "error": "short_summary empty", "step": "validate"}

    # Auto-detect source_kind / id если не передали
    from app.models import TaskSourceKind
    if source_kind is None:
        source_kind = (
            TaskSourceKind.zoom
            if hasattr(row, "zoom_id") and getattr(row, "zoom_id", None)
            else TaskSourceKind.fireflies
        )
    if source_conversation_id is None:
        source_conversation_id = (
            getattr(row, "zoom_id", None)
            or getattr(row, "fireflies_id", None)
            or ""
        )
    row_identifier = (
        getattr(row, "zoom_id", None)
        or getattr(row, "fireflies_id", None)
    )

    # Idempotent skip — если уже postnut в этот channel
    existing_ts = getattr(row, "slack_post_ts", None)
    if existing_ts:
        log.info("slack_publish_skipped_already_posted",
                 row_id=row_identifier,
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
    from ops._send_helpers import build_parent_raw
    from app.services.slack_mirror import (
        SLACK_TEXT_CHUNK_CHARS, _compact_for_slack,
        _split_for_slack, _to_slack_mrkdwn,
    )
    from app.fireflies.pipeline import _build_todo_section

    body, reused_todo = _split_short_summary(short_summary_text)

    # FR-CR-05-199 — два списка задач:
    #   * parent_tasks_text — для «TODO:» trailer в parent message
    #     (фильтр: DIRECTIONS_IMPORTANT, как было всегда)
    #   * thread_tasks_text — для thread reply
    parent_tasks_text = ""
    thread_tasks_text = ""
    if not no_tasks:
        if override_thread_todo_text is not None:
            # V2 publish — задачи переданы прямо текстом (в памяти),
            # БД не читаем. Используется и для parent и для thread.
            parent_tasks_text = override_thread_todo_text
            thread_tasks_text = override_thread_todo_text
        elif use_db_tasks:
            parent_tasks_text = _build_todo_section(
                session,
                source_kind=source_kind,
                source_conversation_id=source_conversation_id,
                filter_by_direction=True,
            )
            if all_tasks_in_thread:
                thread_tasks_text = _build_todo_section(
                    session,
                    source_kind=source_kind,
                    source_conversation_id=source_conversation_id,
                    filter_by_direction=False,
                )
            else:
                thread_tasks_text = parent_tasks_text
        elif reused_todo:
            parent_tasks_text = reused_todo
            thread_tasks_text = reused_todo
    if parent_tasks_text:
        parent_tasks_text = _compact_for_slack(_to_slack_mrkdwn(parent_tasks_text))
    if thread_tasks_text:
        thread_tasks_text = _compact_for_slack(_to_slack_mrkdwn(thread_tasks_text))

    parent_raw = build_parent_raw(body, parent_tasks_text)
    parent_text = _compact_for_slack(_to_slack_mrkdwn(parent_raw))
    chunks = _split_for_slack(parent_text, limit=SLACK_TEXT_CHUNK_CHARS)

    log.info("slack_publish_starting",
             row_id=row_identifier,
             source_kind=str(source_kind),
             channel=channel, chunks=len(chunks),
             has_parent_tasks=bool(parent_tasks_text),
             has_thread_tasks=bool(thread_tasks_text),
             all_tasks_in_thread=all_tasks_in_thread)

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
        if thread_tasks_text:
            client.chat_postMessage(
                channel=channel, text=thread_tasks_text,
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
                    row_id=row_identifier, error=err)
        return {"ok": False, "error": err, "step": "post"}

    # Persist post_ts для idempotency (если не пропущено — V2 publish
    # в read-only mode не трогает БД).
    if not skip_db_write and hasattr(row, "slack_post_ts"):
        row.slack_post_ts = parent_ts
        session.flush()

    log.info("slack_publish_done",
             row_id=row_identifier,
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
