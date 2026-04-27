from __future__ import annotations

from typing import Any

from slack_bolt import Ack
from slack_sdk import WebClient

from app.db import session_scope
from app.logging_setup import get_logger
from app.models import ActionDraft
from app.schemas.intent import InvocationType
from app.slack_bot import blocks as bk
from app.slack_bot.handlers.shared import (
    Services,
    classify_and_persist,
    draft_private_metadata,
    fetch_permalink,
)

log = get_logger(__name__)

SHORTCUT_CREATE_TASK = "create_task_from_message"
SHORTCUT_CREATE_MEETING = "create_meeting_from_message"


def _source_message_from_shortcut(payload: dict[str, Any]) -> dict[str, Any]:
    message = payload.get("message") or {}
    return {
        "ts": message.get("ts"),
        "thread_ts": message.get("thread_ts"),
        "user": message.get("user"),
        "text": message.get("text") or "",
    }


def handle_shortcut(
    *,
    shortcut: dict[str, Any],
    client: WebClient,
    services: Services,
    ack: Ack,
) -> None:
    """Message shortcut: Create task / meeting from message.

    Must be acknowledged within 3s, then open a modal using trigger_id.
    """
    ack()

    trigger_id = shortcut.get("trigger_id")
    callback_id = shortcut.get("callback_id")
    if not trigger_id:
        log.warning("shortcut_missing_trigger_id", callback_id=callback_id)
        return

    channel_id = (shortcut.get("channel") or {}).get("id")
    source_message = _source_message_from_shortcut(shortcut)
    user_id = (shortcut.get("user") or {}).get("id")

    draft: ActionDraft | None = None
    metadata = ""

    with session_scope() as session:
        if channel_id and source_message.get("ts"):
            classification, draft, snapshot = classify_and_persist(
                session,
                services=services,
                conversation_id=channel_id,
                kind="channel",
                source_message=source_message,
                invocation_type=InvocationType.shortcut,
                slack_user_id=user_id,
            )
            permalink = fetch_permalink(
                client, channel=channel_id, ts=source_message["ts"]
            )
            metadata = draft_private_metadata(
                conversation_id=channel_id,
                message_ts=source_message["ts"],
                thread_ts=source_message.get("thread_ts"),
                draft_id=draft.id if draft else None,
                context_snapshot_id=snapshot.id,
                source_user_id=source_message.get("user"),
                permalink=permalink,
            )
            initial = (draft.payload if draft else None) or _seed_from_classification(
                callback_id, classification
            )
        else:
            initial = None
            metadata = draft_private_metadata(
                conversation_id=channel_id or "",
                message_ts=source_message.get("ts") or "",
                thread_ts=source_message.get("thread_ts"),
                draft_id=None,
                context_snapshot_id=None,
                source_user_id=user_id,
                permalink=None,
            )

    if callback_id == SHORTCUT_CREATE_TASK:
        from app.config import get_settings

        view = bk.task_modal(
            private_metadata=metadata,
            initial=initial,
            allowed_owners=get_settings().allowed_owners(),
        )
    elif callback_id == SHORTCUT_CREATE_MEETING:
        # FR-CR-04-19: meetings are out of scope. The shortcut is
        # still registered (legacy app manifests reference it) but
        # we surface a polite "tasks-only" notice instead of opening
        # the meeting modal.
        try:
            client.chat_postEphemeral(
                channel=channel_id or user_id or "",
                user=user_id or "",
                text=":information_source: This bot only handles tasks now. Try the *Create task* shortcut.",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("meeting_shortcut_disabled_notice_failed", error=str(e))
        return
    else:
        log.warning("unknown_shortcut_callback_id", callback_id=callback_id)
        return

    client.views_open(trigger_id=trigger_id, view=view)


def _seed_from_classification(callback_id: str | None, classification) -> dict[str, Any] | None:
    if classification is None:
        return None
    if callback_id == SHORTCUT_CREATE_TASK and classification.task:
        return classification.task.model_dump(mode="json")
    if callback_id == SHORTCUT_CREATE_MEETING and classification.meeting:
        return classification.meeting.model_dump(mode="json")
    return None
