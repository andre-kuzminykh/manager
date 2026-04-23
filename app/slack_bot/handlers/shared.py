"""Shared helpers used by multiple Slack handlers."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from slack_sdk import WebClient
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.context import ContextRetriever
from app.db import session_scope
from app.intent import IntentClassifier
from app.models import ActionDraft, ContextSnapshot, SlackConversation, SlackMessage
from app.orchestrator import Orchestrator
from app.schemas.intent import IntentClassification, InvocationType


@dataclass
class Services:
    slack: WebClient
    context_retriever: ContextRetriever
    classifier: IntentClassifier
    orchestrator: Orchestrator


def upsert_conversation(session: Session, *, channel_id: str, kind: str) -> SlackConversation:
    record = session.get(SlackConversation, channel_id)
    if record is not None:
        return record

    dialect = session.bind.dialect.name if session.bind is not None else ""
    if dialect == "postgresql":
        stmt = (
            pg_insert(SlackConversation)
            .values(id=channel_id, kind=kind)
            .on_conflict_do_nothing(index_elements=["id"])
        )
        session.execute(stmt)
        session.flush()
        return session.get(SlackConversation, channel_id)  # type: ignore[return-value]

    # sqlite / generic: savepoint + re-fetch on conflict.
    sp = session.begin_nested()
    try:
        record = SlackConversation(id=channel_id, kind=kind)
        session.add(record)
        session.flush()
    except IntegrityError:
        sp.rollback()
        record = session.get(SlackConversation, channel_id)  # type: ignore[assignment]
    else:
        sp.commit()
    return record  # type: ignore[return-value]


def upsert_message(
    session: Session,
    *,
    conversation: SlackConversation,
    message: dict[str, Any],
    raw: dict[str, Any] | None = None,
) -> SlackMessage:
    ts = message["ts"]
    existing = (
        session.query(SlackMessage)
        .filter_by(conversation_id=conversation.id, ts=ts)
        .one_or_none()
    )
    if existing is not None:
        return existing

    dialect = session.bind.dialect.name if session.bind is not None else ""
    values = {
        "conversation_id": conversation.id,
        "ts": ts,
        "thread_ts": message.get("thread_ts"),
        "user_id": message.get("user") or message.get("bot_id"),
        "text": message.get("text") or "",
        "raw": raw,
    }
    if dialect == "postgresql":
        stmt = (
            pg_insert(SlackMessage)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["conversation_id", "ts"])
        )
        session.execute(stmt)
        session.flush()
        return (
            session.query(SlackMessage)
            .filter_by(conversation_id=conversation.id, ts=ts)
            .one()
        )

    sp = session.begin_nested()
    try:
        record = SlackMessage(**values)
        session.add(record)
        session.flush()
    except IntegrityError:
        sp.rollback()
        record = (
            session.query(SlackMessage)
            .filter_by(conversation_id=conversation.id, ts=ts)
            .one()
        )
    else:
        sp.commit()
    return record


def fetch_permalink(client: WebClient, *, channel: str, ts: str) -> str | None:
    try:
        resp = client.chat_getPermalink(channel=channel, message_ts=ts)
        return resp.get("permalink")
    except Exception:  # noqa: BLE001
        return None


def draft_private_metadata(
    *,
    conversation_id: str,
    message_ts: str,
    thread_ts: str | None,
    draft_id: int | None,
    context_snapshot_id: int | None,
    source_user_id: str | None,
    permalink: str | None,
) -> str:
    return json.dumps(
        {
            "conversation_id": conversation_id,
            "message_ts": message_ts,
            "thread_ts": thread_ts,
            "draft_id": draft_id,
            "context_snapshot_id": context_snapshot_id,
            "source_user_id": source_user_id,
            "permalink": permalink,
        }
    )


def load_private_metadata(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


MENTION_PATTERN = re.compile(r"<@([A-Z0-9]+)>")


def strip_bot_mentions(text: str, bot_user_id: str | None) -> str:
    if not text:
        return ""
    if bot_user_id:
        text = text.replace(f"<@{bot_user_id}>", "")
    return text.strip()


def classify_and_persist(
    session: Session,
    *,
    services: Services,
    conversation_id: str,
    kind: str,
    source_message: dict[str, Any],
    invocation_type: InvocationType,
    slack_user_id: str | None,
) -> tuple[IntentClassification, ActionDraft | None, ContextSnapshot]:
    """End-to-end: load context, classify, persist snapshot + inference + draft."""

    conversation = upsert_conversation(session, channel_id=conversation_id, kind=kind)
    upsert_message(session, conversation=conversation, message=source_message)

    context = services.context_retriever.build(
        conversation_id=conversation_id, source_message=source_message
    )

    classification = services.classifier.classify(
        context=context, invocation_type=invocation_type
    )

    snapshot = services.orchestrator.persist_context_snapshot(
        session, context.to_snapshot_dict()
    )
    inference = services.orchestrator.persist_inference(
        session,
        context_snapshot=snapshot,
        classification=classification,
        invocation_type=invocation_type,
    )

    draft: ActionDraft | None = None
    if classification.draft_payload() is not None:
        draft = services.orchestrator.create_draft(
            session,
            inference=inference,
            classification=classification,
            created_by_slack_user_id=slack_user_id,
            slack_message_ts=source_message.get("ts"),
        )
    return classification, draft, snapshot


def with_session():
    return session_scope()
