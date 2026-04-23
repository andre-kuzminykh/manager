"""Slack event handlers: message.im, message.mpim, message.channels/groups, app_mention."""
from __future__ import annotations

from typing import Any

from slack_bolt import Ack, BoltContext
from slack_sdk import WebClient
from sqlalchemy.orm.attributes import flag_modified

from app.config import get_settings
from app.db import session_scope
from app.logging_setup import get_logger
from app.models import ActionDraft, ActionDraftState
from app.schemas.intent import IntentClassification, IntentType, InvocationType
from app.services import parse_reply, pick_next_missing, prompt_for
from app.slack_bot import blocks as bk
from app.slack_bot.dedup import claim_event
from app.slack_bot.handlers.shared import (
    Services,
    classify_and_persist,
    draft_private_metadata,
    fetch_permalink,
    strip_bot_mentions,
)
from app.slack_bot.rate_limiter import RateAwareSlackSender

log = get_logger(__name__)


def _kind_from_channel_type(channel_type: str | None, channel_id: str | None) -> str:
    if channel_type in ("im", "mpim"):
        return channel_type
    if channel_id and channel_id.startswith("G"):
        return "group"
    return "channel"


def _is_ignorable(event: dict[str, Any], bot_user_id: str | None) -> bool:
    subtype = event.get("subtype")
    if subtype in ("message_changed", "message_deleted", "bot_message", "channel_join"):
        return True
    if event.get("bot_id") and event.get("user") == bot_user_id:
        return True
    if bot_user_id and event.get("user") == bot_user_id:
        return True
    text = event.get("text") or ""
    if not text.strip():
        return True
    # Slack fires both `app_mention` and `message.*` for messages that mention
    # the bot. The explicit mention handler already takes care of those — skip
    # them here to avoid duplicate drafts and races on shared rows.
    if bot_user_id and f"<@{bot_user_id}>" in text:
        return True
    return False


def _build_card_from_payload(draft: ActionDraft) -> list[dict[str, Any]]:
    """Rebuild the draft card from the current payload on the draft row.

    After the user's follow-up fills a field, we chat.update the card so it
    reflects the new data.
    """
    from app.schemas.intent import (
        IntentClassification,
        IntentType,
        MeetingDraft,
        TaskDraft,
    )

    intent_map = {
        "create_task": IntentType.create_task,
        "update_task": IntentType.update_task,
        "create_meeting": IntentType.create_meeting,
        "update_meeting": IntentType.update_meeting,
    }
    intent = intent_map.get(draft.intent.value, IntentType.no_action)
    payload = draft.payload or {}
    if intent in (IntentType.create_task, IntentType.update_task):
        task = TaskDraft.model_validate(payload)
        classification = IntentClassification(
            intent=intent, confidence=0.9, task=task
        )
    elif intent in (IntentType.create_meeting, IntentType.update_meeting):
        meeting = MeetingDraft.model_validate(payload)
        classification = IntentClassification(
            intent=intent, confidence=0.9, meeting=meeting
        )
    else:
        classification = IntentClassification(intent=intent, confidence=0.9)

    missing = _missing_fields(classification)
    return bk.draft_card(
        classification=classification,
        draft_id=draft.id,
        confidence_bucket="high",
        missing_fields=missing,
    )


def _handle_followup_reply(
    *,
    session,
    thread_ts: str,
    user_id: str | None,
    reply_text: str,
    sender: RateAwareSlackSender,
    client: WebClient,
) -> bool:
    """Try to interpret a thread reply as the answer to a pending follow-up.

    Returns True if it was consumed as a follow-up (caller should stop), or
    False if it should fall through to the regular passive path.
    """
    draft = (
        session.query(ActionDraft)
        .filter(
            ActionDraft.slack_message_ts == thread_ts,
            ActionDraft.awaiting_field.isnot(None),
            ActionDraft.state == ActionDraftState.proposed,
        )
        .order_by(ActionDraft.id.desc())
        .first()
    )
    if draft is None:
        return False

    field = draft.awaiting_field
    parsed = parse_reply(
        field=field,
        reply_text=reply_text,
        settings=get_settings(),
    )
    if parsed is None:
        sender.post_message(
            channel=draft.card_channel or "",
            thread_ts=thread_ts,
            text=(
                f":question: Не распарсил ответ как *{field}*. "
                f"{prompt_for(field)} Или нажми *Edit* на карточке."
            ),
        )
        return True

    # Merge into draft payload.
    payload = dict(draft.payload or {})
    payload.update(parsed)
    draft.payload = payload
    flag_modified(draft, "payload")

    # Decide what to ask next.
    next_field = pick_next_missing(draft.intent.value, payload)
    draft.awaiting_field = next_field
    session.flush()

    # Update the card in place.
    if draft.card_channel and draft.card_ts:
        try:
            sender.update_message(
                channel=draft.card_channel,
                ts=draft.card_ts,
                blocks=_build_card_from_payload(draft),
                text="Action draft",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("card_update_failed", error=str(e), draft_id=draft.id)

    # Ack in thread — plus next question if there is one.
    if next_field:
        sender.post_message(
            channel=draft.card_channel or "",
            thread_ts=thread_ts,
            text=f":ok_hand: Записал. {prompt_for(next_field)}",
        )
    else:
        sender.post_message(
            channel=draft.card_channel or "",
            thread_ts=thread_ts,
            text=":white_check_mark: Все поля собрал. Жми *Confirm* на карточке.",
        )
    return True


def _missing_fields(classification: IntentClassification) -> list[str]:
    """Return required fields that are still empty — for inline prompts."""
    missing: list[str] = []
    if classification.intent in (IntentType.create_task, IntentType.update_task):
        t = classification.task
        if t is None or not t.title:
            missing.append("title")
        if t is None or not t.owner_display_name and not t.owner_user_id:
            missing.append("owner")
        if t is None or t.due_date is None:
            missing.append("due date")
    elif classification.intent in (IntentType.create_meeting, IntentType.update_meeting):
        m = classification.meeting
        if m is None or not m.title:
            missing.append("title")
        if m is None or not m.participants:
            missing.append("participants")
        if m is None or m.datetime_at is None:
            missing.append("date/time")
    return missing


def handle_message(
    *,
    event: dict[str, Any],
    body: dict[str, Any],
    client: WebClient,
    context: BoltContext,
    services: Services,
    sender: RateAwareSlackSender,
    ack: Ack,
) -> None:
    """Handle message.im / message.mpim / message.channels events (passive mode)."""
    ack()

    event_id = body.get("event_id") or ""
    bot_user_id = context.bot_user_id

    if _is_ignorable(event, bot_user_id):
        return

    channel = event.get("channel")
    if not channel:
        return

    with session_scope() as session:
        if not claim_event(session, event_id):
            log.info("duplicate_event_skipped", event_id=event_id)
            return

        # If this is a reply inside a thread where we have a pending draft
        # awaiting the user's answer, route it into the follow-up flow.
        thread_ts = event.get("thread_ts")
        reply_text = strip_bot_mentions(event.get("text", ""), bot_user_id)
        if thread_ts and reply_text and event.get("user") != bot_user_id:
            if _handle_followup_reply(
                session=session,
                thread_ts=thread_ts,
                user_id=event.get("user"),
                reply_text=reply_text,
                sender=sender,
                client=client,
            ):
                return

        kind = _kind_from_channel_type(event.get("channel_type"), channel)
        text = reply_text  # already cleaned

        source_message = {
            "ts": event["ts"],
            "thread_ts": thread_ts,
            "user": event.get("user"),
            "text": text,
        }

        classification, draft, snapshot = classify_and_persist(
            session,
            services=services,
            conversation_id=channel,
            kind=kind,
            source_message=source_message,
            invocation_type=InvocationType.passive,
            slack_user_id=event.get("user"),
        )

        decision = services.orchestrator.decide_passive(
            classification=classification, draft_id=draft.id if draft else None
        )

        if decision.action == "silent" or draft is None:
            return

        permalink = fetch_permalink(client, channel=channel, ts=event["ts"])
        metadata = draft_private_metadata(
            conversation_id=channel,
            message_ts=event["ts"],
            thread_ts=event.get("thread_ts"),
            draft_id=draft.id,
            context_snapshot_id=snapshot.id,
            source_user_id=event.get("user"),
            permalink=permalink,
        )

        if decision.action == "card":
            payload = bk.draft_card(
                classification=classification,
                draft_id=draft.id,
                confidence_bucket=decision.confidence_bucket.value,
                missing_fields=_missing_fields(classification),
            )
        else:
            payload = bk.soft_prompt(classification.intent, draft.id)

        sender.post_message(
            channel=channel,
            thread_ts=event.get("thread_ts") or event["ts"],
            blocks=payload,
            text="Action suggestion",
            metadata={"event_type": "draft", "event_payload": {"metadata": metadata}},
        )


def handle_app_mention(
    *,
    event: dict[str, Any],
    body: dict[str, Any],
    client: WebClient,
    context: BoltContext,
    services: Services,
    sender: RateAwareSlackSender,
    ack: Ack,
) -> None:
    """Explicit @mention flow: always return a user-visible reply.

    Contract: the bot MUST post something back to the channel for every
    mention. Either a draft card (with a "missing fields" hint when relevant)
    or an error/fallback message — never silence.
    """
    ack()

    channel = event.get("channel")
    thread_ts = event.get("thread_ts") or event.get("ts")

    def _reply(text: str) -> None:
        if channel:
            try:
                sender.post_message(channel=channel, thread_ts=thread_ts, text=text)
            except Exception as e:  # noqa: BLE001
                log.error("mention_fallback_reply_failed", error=str(e))

    if not channel:
        log.warning("mention_event_without_channel")
        return

    event_id = body.get("event_id") or ""
    bot_user_id = context.bot_user_id

    try:
        with session_scope() as session:
            if not claim_event(session, event_id):
                log.info("duplicate_event_skipped", event_id=event_id)
                return

            text = strip_bot_mentions(event.get("text", ""), bot_user_id)
            source_message = {
                "ts": event["ts"],
                "thread_ts": event.get("thread_ts"),
                "user": event.get("user"),
                "text": text,
            }

            classification, draft, snapshot = classify_and_persist(
                session,
                services=services,
                conversation_id=channel,
                kind=_kind_from_channel_type(event.get("channel_type"), channel),
                source_message=source_message,
                invocation_type=InvocationType.mention,
                slack_user_id=event.get("user"),
            )

            permalink = fetch_permalink(client, channel=channel, ts=event["ts"])

            if draft is None:
                _reply(
                    ":thinking_face: Не понял, что создать. "
                    "Попробуй: `@bot создай задачу: <что сделать> до <когда>, ответственный <кто>` "
                    "или: `@bot создай встречу с <кем> <когда>`."
                )
                return

            metadata = draft_private_metadata(
                conversation_id=channel,
                message_ts=event["ts"],
                thread_ts=event.get("thread_ts"),
                draft_id=draft.id,
                context_snapshot_id=snapshot.id,
                source_user_id=event.get("user"),
                permalink=permalink,
            )

            missing = _missing_fields(classification)
            post_resp = sender.post_message(
                channel=channel,
                thread_ts=thread_ts,
                blocks=bk.draft_card(
                    classification=classification,
                    draft_id=draft.id,
                    confidence_bucket="high",
                    missing_fields=missing,
                ),
                text="Action draft",
                metadata={"event_type": "draft", "event_payload": {"metadata": metadata}},
            )

            # Remember where the card lives so a later thread reply can
            # chat.update it. And queue a follow-up question for the first
            # missing field.
            draft.card_channel = channel
            draft.card_ts = post_resp.get("ts") if isinstance(post_resp, dict) else None
            next_field = pick_next_missing(classification.intent.value, draft.payload)
            draft.awaiting_field = next_field
            session.flush()

            if next_field:
                sender.post_message(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=f":speech_balloon: {prompt_for(next_field)}",
                )
    except Exception as e:  # noqa: BLE001 — mention must never go silent
        log.exception("mention_handler_failed", error=str(e))
        _reply(f":warning: что-то сломалось при обработке: `{e!s}` — посмотри логи бота.")
