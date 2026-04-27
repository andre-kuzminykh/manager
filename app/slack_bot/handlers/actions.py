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


def _delete_draft_card(
    sender: RateAwareSlackSender, body: dict[str, Any]
) -> None:
    """Remove the draft widget from the thread so it stops cluttering UI
    after the user made a decision."""
    channel = (body.get("channel") or {}).get("id")
    ts = (body.get("message") or {}).get("ts")
    if not channel or not ts:
        return
    try:
        sender.delete_message(channel=channel, ts=ts)
    except Exception as e:  # noqa: BLE001 — best effort, not fatal
        log.warning("draft_card_delete_failed", error=str(e))


def _cleanup_follow_up_messages(
    sender: RateAwareSlackSender, draft_id: int
) -> None:
    """Delete every follow-up question / ack the bot posted in the thread
    so the history under the widget is tidy after Confirm / Ignore."""
    with session_scope() as session:
        draft = session.get(ActionDraft, draft_id)
        if draft is None:
            return
        channel = draft.card_channel
        tss = list(draft.follow_up_message_ts or [])
        # Clear so retries don't double-delete.
        draft.follow_up_message_ts = []

    if not channel or not tss or not hasattr(sender, "delete_message"):
        return

    for ts in tss:
        try:
            sender.delete_message(channel=channel, ts=ts)
        except Exception as e:  # noqa: BLE001
            log.warning("follow_up_delete_failed", ts=ts, error=str(e))


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
        # On success the widget itself was morphed into a task card
        # via chat.update (see FinalizeService._morph_widget_into_task_card).
        # Clean up the follow-up Q&A in the thread so only the live card
        # remains visible.
        _cleanup_follow_up_messages(sender, draft_id)
    except Exception as e:  # noqa: BLE001
        log.error("finalize_failed", error=str(e), draft_id=draft_id)
        if channel:
            sender.post_message(
                channel=channel,
                thread_ts=thread_ts,
                blocks=bk.failure_message("entity", str(e)),
                text="Failed to create entity",
            )
        # On failure keep the widget so the user can retry via Edit / Confirm.
        return

    # Passive-accept parity with @mention: if the just-created task is
    # still missing a user-visible field (owner / due_date / …), post
    # a follow-up question in the same thread so the author can answer
    # in place. The reply handler updates the Task and refreshes the
    # card — see _handle_followup_reply.
    if entity_type == "task":
        _post_accept_follow_up(
            task_id=entity_id,
            draft_id=draft_id,
            channel=channel,
            thread_ts=thread_ts,
            sender=sender,
        )


def _post_accept_follow_up(
    *,
    task_id: int,
    draft_id: int,
    channel: str | None,
    thread_ts: str | None,
    sender: RateAwareSlackSender,
) -> None:
    from app.config import get_settings
    from app.models import Task
    from app.services import pick_next_missing, prompt_for
    from app.slack_bot.handlers.events import (
        _record_followup_ts,
        _task_payload,
    )

    if not channel:
        return
    with session_scope() as session:
        task = session.get(Task, task_id)
        draft = session.get(ActionDraft, draft_id)
        if task is None or draft is None:
            return
        next_field = pick_next_missing("create_task", _task_payload(task))
        draft.awaiting_field = next_field
        session.flush()
        if not next_field:
            return
        intro = (
            f":memo: Captured: *{task.title}*.\n"
            + prompt_for(
                next_field,
                payload=_task_payload(task),
                allowed_owners=get_settings().allowed_owners(),
            )
        )
        try:
            resp = sender.post_message(
                channel=channel,
                thread_ts=thread_ts,
                text=intro,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("accept_followup_post_failed", error=str(e))
            return
        _record_followup_ts(session, draft, resp)


def handle_ignore(
    *,
    body: dict[str, Any],
    ack: Ack,
    sender: RateAwareSlackSender | None = None,
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
    if sender is not None:
        _delete_draft_card(sender, body)
        _cleanup_follow_up_messages(sender, draft_id)


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
        from app.services.owners import list_known_owners

        allowed_owners = list_known_owners(session)

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
        view = bk.task_modal(
            private_metadata=pm,
            initial=payload,
            allowed_owners=allowed_owners,
        )
    else:
        view = bk.meeting_modal(private_metadata=pm, initial=payload)

    client.views_open(trigger_id=trigger_id, view=view)
