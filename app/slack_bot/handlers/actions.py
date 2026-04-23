"""Button action handlers: Confirm / Edit / Ignore on draft cards."""
from __future__ import annotations

from typing import Any

from slack_bolt import Ack
from slack_sdk import WebClient

from app.db import session_scope
from app.logging_setup import get_logger
from app.models import ActionDraft, ActionDraftState
from app.models.intent import IntentType as IntentTypeEnum
from app.orchestrator.finalize import FinalizeService
from app.slack_bot import blocks as bk
from app.slack_bot.handlers.shared import (
    Services,
    draft_private_metadata,
    load_private_metadata,
)
from app.slack_bot.rate_limiter import RateAwareSlackSender

log = get_logger(__name__)


def _extract_metadata_from_action(body: dict[str, Any]) -> dict[str, Any]:
    metadata = body.get("message", {}).get("metadata", {})
    payload = metadata.get("event_payload") or {}
    return load_private_metadata(payload.get("metadata"))


def handle_confirm(
    *,
    body: dict[str, Any],
    client: WebClient,
    services: Services,
    finalizer: FinalizeService,
    sender: RateAwareSlackSender,
    ack: Ack,
) -> None:
    ack()
    action = (body.get("actions") or [{}])[0]
    draft_id = int(action.get("value") or 0)
    if not draft_id:
        return

    metadata = _extract_metadata_from_action(body)
    channel = body.get("channel", {}).get("id")
    thread_ts = metadata.get("thread_ts") or metadata.get("message_ts")

    try:
        entity_type, entity_id, summary = finalizer.finalize_draft(
            draft_id=draft_id, source_metadata=metadata
        )
        if channel:
            sender.post_message(
                channel=channel,
                thread_ts=thread_ts,
                blocks=bk.success_message(entity_type, entity_id, summary),
                text=f"{entity_type} created",
            )
    except Exception as e:  # noqa: BLE001
        log.error("finalize_failed", error=str(e), draft_id=draft_id)
        if channel:
            sender.post_message(
                channel=channel,
                thread_ts=thread_ts,
                blocks=bk.failure_message("entity", str(e)),
                text="Failed to create entity",
            )


def handle_ignore(
    *,
    body: dict[str, Any],
    ack: Ack,
) -> None:
    ack()
    action = (body.get("actions") or [{}])[0]
    draft_id = int(action.get("value") or 0)
    if not draft_id:
        return
    with session_scope() as session:
        draft = session.get(ActionDraft, draft_id)
        if draft and draft.state == ActionDraftState.proposed:
            draft.state = ActionDraftState.ignored


def handle_edit(
    *,
    body: dict[str, Any],
    client: WebClient,
    ack: Ack,
) -> None:
    """Open a modal prefilled from the draft payload."""
    ack()

    action = (body.get("actions") or [{}])[0]
    draft_id = int(action.get("value") or 0)
    trigger_id = body.get("trigger_id")
    if not draft_id or not trigger_id:
        return

    metadata = _extract_metadata_from_action(body)

    with session_scope() as session:
        draft = session.get(ActionDraft, draft_id)
        if draft is None:
            log.warning("edit_unknown_draft", draft_id=draft_id)
            return
        intent = draft.intent
        payload = dict(draft.payload or {})

    pm = draft_private_metadata(
        conversation_id=metadata.get("conversation_id", ""),
        message_ts=metadata.get("message_ts", ""),
        thread_ts=metadata.get("thread_ts"),
        draft_id=draft_id,
        context_snapshot_id=metadata.get("context_snapshot_id"),
        source_user_id=metadata.get("source_user_id"),
        permalink=metadata.get("permalink"),
    )

    if intent in (IntentTypeEnum.create_task, IntentTypeEnum.update_task):
        from app.config import get_settings

        view = bk.task_modal(
            private_metadata=pm,
            initial=payload,
            allowed_owners=get_settings().allowed_owners(),
        )
    else:
        view = bk.meeting_modal(private_metadata=pm, initial=payload)

    client.views_open(trigger_id=trigger_id, view=view)
