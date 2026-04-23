"""Modal submission handlers."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from slack_bolt import Ack
from slack_sdk import WebClient

from app.db import session_scope
from app.logging_setup import get_logger
from app.models import ActionDraft, ActionDraftState, IntentInference
from app.models.intent import IntentType as IntentTypeEnum
from app.orchestrator.finalize import FinalizeService
from app.slack_bot import blocks as bk
from app.slack_bot.handlers.shared import Services, load_private_metadata
from app.slack_bot.rate_limiter import RateAwareSlackSender

log = get_logger(__name__)


def _state_value(values: dict[str, Any], block_id: str, action_id: str) -> Any:
    block = values.get(block_id, {})
    element = block.get(action_id, {})
    if "value" in element:
        return element.get("value")
    if "selected_option" in element:
        sel = element.get("selected_option") or {}
        return sel.get("value")
    if "selected_date" in element:
        return element.get("selected_date")
    if "selected_date_time" in element:
        return element.get("selected_date_time")
    return None


def _extract_task_payload(view: dict[str, Any]) -> dict[str, Any]:
    values = view.get("state", {}).get("values", {})
    owner_raw = _state_value(values, bk.BLOCK_OWNER, bk.INPUT_OWNER)
    # static_select case returns a Slack user id; plain_text_input returns a name.
    owner_is_slack_id = isinstance(owner_raw, str) and owner_raw.startswith(("U", "W"))
    effort_raw = _state_value(values, bk.BLOCK_EFFORT, bk.INPUT_EFFORT)
    estimated_minutes: int | None = None
    if effort_raw:
        try:
            estimated_minutes = int(str(effort_raw).strip())
        except (TypeError, ValueError):
            estimated_minutes = None

    return {
        "title": (_state_value(values, bk.BLOCK_TITLE, bk.INPUT_TITLE) or "").strip(),
        "description": _state_value(values, bk.BLOCK_DESCRIPTION, bk.INPUT_DESCRIPTION),
        "owner_user_id": owner_raw if owner_is_slack_id else None,
        "owner_display_name": None if owner_is_slack_id else owner_raw,
        "priority": _state_value(values, bk.BLOCK_PRIORITY, bk.INPUT_PRIORITY) or "medium",
        "due_date": _state_value(values, bk.BLOCK_DUE, bk.INPUT_DUE),
        "estimated_minutes": estimated_minutes,
    }


def _extract_meeting_payload(view: dict[str, Any]) -> dict[str, Any]:
    values = view.get("state", {}).get("values", {})
    participants_raw = _state_value(values, bk.BLOCK_PARTICIPANTS, bk.INPUT_PARTICIPANTS) or ""
    participants = [p.strip() for p in participants_raw.split(",") if p.strip()]

    datetime_ts = _state_value(values, bk.BLOCK_DATETIME, bk.INPUT_DATETIME)
    datetime_at = None
    if datetime_ts:
        datetime_at = datetime.fromtimestamp(int(datetime_ts), tz=timezone.utc).isoformat()

    return {
        "title": (_state_value(values, bk.BLOCK_TITLE, bk.INPUT_TITLE) or "").strip(),
        "participants": participants,
        "datetime_at": datetime_at,
        "notes": _state_value(values, bk.BLOCK_NOTES, bk.INPUT_NOTES),
    }


def _post_feedback(
    sender: RateAwareSlackSender,
    *,
    metadata: dict[str, Any],
    blocks: list[dict[str, Any]],
    text: str,
) -> None:
    channel = metadata.get("conversation_id")
    if not channel:
        return
    thread_ts = metadata.get("thread_ts") or metadata.get("message_ts")
    sender.post_message(channel=channel, thread_ts=thread_ts, blocks=blocks, text=text)


def _ensure_draft_for_submission(
    session, *, metadata: dict[str, Any], intent: IntentTypeEnum, payload: dict[str, Any]
) -> ActionDraft:
    """Return an existing draft referenced by metadata, or create a freestanding one."""
    draft_id = metadata.get("draft_id")
    if draft_id:
        draft = session.get(ActionDraft, int(draft_id))
        if draft is not None:
            draft.payload = payload
            draft.state = ActionDraftState.edited
            session.flush()
            return draft

    # Freestanding draft (e.g. shortcut on a message the bot hasn't seen before).
    inference = IntentInference(
        context_snapshot_id=metadata.get("context_snapshot_id"),
        intent=intent,
        confidence=1.0,
        invocation_type="shortcut",
        reasoning="manual submit without prior detection",
    )
    session.add(inference)
    session.flush()

    draft = ActionDraft(
        inference_id=inference.id,
        intent=intent,
        state=ActionDraftState.edited,
        payload=payload,
        created_by_slack_user_id=metadata.get("source_user_id"),
        slack_message_ts=metadata.get("message_ts"),
    )
    session.add(draft)
    session.flush()
    return draft


def handle_task_modal_submit(
    *,
    body: dict[str, Any],
    view: dict[str, Any],
    services: Services,
    finalizer: FinalizeService,
    sender: RateAwareSlackSender,
    ack: Ack,
) -> None:
    payload = _extract_task_payload(view)
    if not payload["title"]:
        ack(response_action="errors", errors={bk.BLOCK_TITLE: "Title is required"})
        return
    ack()

    metadata = load_private_metadata(view.get("private_metadata"))

    with session_scope() as session:
        draft = _ensure_draft_for_submission(
            session,
            metadata=metadata,
            intent=IntentTypeEnum.create_task,
            payload=payload,
        )
        draft_id = draft.id

    try:
        entity_type, entity_id, summary = finalizer.finalize_draft(
            draft_id=draft_id, source_metadata=metadata
        )
        _post_feedback(
            sender,
            metadata=metadata,
            blocks=bk.success_message(
                entity_type, entity_id, summary, permalink=metadata.get("permalink")
            ),
            text=f"{entity_type} created",
        )
    except Exception as e:  # noqa: BLE001
        log.error("finalize_task_modal_failed", error=str(e), draft_id=draft_id)
        _post_feedback(
            sender,
            metadata=metadata,
            blocks=bk.failure_message("task", str(e)),
            text="Failed to create task",
        )


def handle_meeting_modal_submit(
    *,
    body: dict[str, Any],
    view: dict[str, Any],
    services: Services,
    finalizer: FinalizeService,
    sender: RateAwareSlackSender,
    ack: Ack,
) -> None:
    payload = _extract_meeting_payload(view)
    errors: dict[str, str] = {}
    if not payload["title"]:
        errors[bk.BLOCK_TITLE] = "Title is required"
    if not payload["datetime_at"]:
        errors[bk.BLOCK_DATETIME] = "Please pick a date and time"
    if errors:
        ack(response_action="errors", errors=errors)
        return
    ack()

    metadata = load_private_metadata(view.get("private_metadata"))

    with session_scope() as session:
        draft = _ensure_draft_for_submission(
            session,
            metadata=metadata,
            intent=IntentTypeEnum.create_meeting,
            payload=payload,
        )
        draft_id = draft.id

    try:
        entity_type, entity_id, summary = finalizer.finalize_draft(
            draft_id=draft_id, source_metadata=metadata
        )
        _post_feedback(
            sender,
            metadata=metadata,
            blocks=bk.success_message(
                entity_type, entity_id, summary, permalink=metadata.get("permalink")
            ),
            text=f"{entity_type} created",
        )
    except Exception as e:  # noqa: BLE001
        log.error("finalize_meeting_modal_failed", error=str(e), draft_id=draft_id)
        _post_feedback(
            sender,
            metadata=metadata,
            blocks=bk.failure_message("meeting", str(e)),
            text="Failed to create meeting",
        )
