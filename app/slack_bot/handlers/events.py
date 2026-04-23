"""Slack event handlers: message.im, message.mpim, message.channels/groups, app_mention."""
from __future__ import annotations

from typing import Any

from slack_bolt import Ack, BoltContext
from slack_sdk import WebClient

from app.db import session_scope
from app.logging_setup import get_logger
from app.schemas.intent import InvocationType
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
    # Ignore edits, deletes and bot self messages.
    subtype = event.get("subtype")
    if subtype in ("message_changed", "message_deleted", "bot_message", "channel_join"):
        return True
    if event.get("bot_id") and event.get("user") == bot_user_id:
        return True
    if bot_user_id and event.get("user") == bot_user_id:
        return True
    if not (event.get("text") or "").strip():
        return True
    return False


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

        kind = _kind_from_channel_type(event.get("channel_type"), channel)
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
    """Explicit @mention flow: always return a confirmation card."""
    ack()

    event_id = body.get("event_id") or ""
    bot_user_id = context.bot_user_id
    channel = event.get("channel")
    if not channel:
        return

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
            sender.post_message(
                channel=channel,
                thread_ts=event.get("thread_ts") or event["ts"],
                text=(
                    "I couldn't detect a task or meeting in that message. "
                    "Try something like: `@bot создай задачу: подготовить список фондов до пятницы`."
                ),
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

        sender.post_message(
            channel=channel,
            thread_ts=event.get("thread_ts") or event["ts"],
            blocks=bk.draft_card(
                classification=classification,
                draft_id=draft.id,
                confidence_bucket="high",
            ),
            text="Action draft",
            metadata={"event_type": "draft", "event_payload": {"metadata": metadata}},
        )
