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
from app.models import (
    ActionDraft,
    ContextSnapshot,
    SlackConversation,
    SlackEventArchive,
    SlackMessage,
)
from app.orchestrator import Orchestrator
from app.schemas.intent import IntentClassification, InvocationType
from app.services import EmployeeDirectory


@dataclass
class Services:
    slack: WebClient
    context_retriever: ContextRetriever
    classifier: IntentClassifier
    orchestrator: Orchestrator
    employees: EmployeeDirectory | None = None


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
    transcript: str | None = None,
    has_audio: bool = False,
    subtype: str | None = None,
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
        "subtype": subtype or message.get("subtype"),
        "text": message.get("text") or "",
        "transcript": transcript,
        "has_audio": has_audio,
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


def archive_event(
    session: Session,
    *,
    event: dict[str, Any],
    body: dict[str, Any] | None = None,
    transcript: str | None = None,
) -> None:
    """Append a row to slack_events_archive.

    Captures EVERY Slack event the bot received — including ones that
    `_is_ignorable` will later drop (message_changed, message_deleted,
    bot_message, channel_join, mentions in passive path). The full
    Slack event JSON is stored verbatim in `raw`.

    Best-effort: a failure here should not abort event handling.
    """
    try:
        record = SlackEventArchive(
            event_id=(body or {}).get("event_id"),
            event_type=event.get("type") or "unknown",
            subtype=event.get("subtype"),
            conversation_id=event.get("channel"),
            ts=event.get("ts"),
            thread_ts=event.get("thread_ts"),
            user_id=event.get("user") or event.get("bot_id"),
            text=event.get("text") or "",
            transcript=transcript,
            raw=event,
        )
        session.add(record)
        session.flush()
    except Exception:  # noqa: BLE001 — never block on the archive
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass


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
    raw_event: dict[str, Any] | None = None,
    transcript: str | None = None,
    has_audio: bool = False,
) -> tuple[IntentClassification, ActionDraft | None, ContextSnapshot]:
    """End-to-end: load context, classify, persist snapshot + inference + draft."""

    conversation = upsert_conversation(session, channel_id=conversation_id, kind=kind)
    upsert_message(
        session,
        conversation=conversation,
        message=source_message,
        raw=raw_event,
        transcript=transcript,
        has_audio=has_audio,
        subtype=source_message.get("subtype"),
    )

    # CR-03: keep the Employees directory fresh for everyone we see.
    if services.employees is not None and slack_user_id:
        try:
            services.employees.observed(session, slack_user_id=slack_user_id)
        except Exception:  # noqa: BLE001 — directory is best-effort
            pass

    context = services.context_retriever.build(
        conversation_id=conversation_id, source_message=source_message
    )

    # Pull the team directory so the owner LLM stage can map names
    # like "Иван" to a real Slack user id from the same table.
    from app.models import Employee

    known_employees = [
        {
            "slack_user_id": e.slack_user_id,
            "display_name": e.display_name or e.real_name or e.slack_user_id,
            "real_name": e.real_name,
        }
        for e in (
            session.query(Employee)
            .filter(Employee.is_bot.is_(False))
            .all()
        )
    ]

    classification = services.classifier.classify(
        context=context,
        invocation_type=invocation_type,
        known_employees=known_employees,
    )

    # If the pipeline still couldn't identify an owner — neither a real
    # slack_user_id nor a name we could resolve — fall back to the
    # message author. We DO NOT overwrite when the LLM emitted a
    # display_name without a slack_user_id: that means it found a name
    # we don't know yet, and the bot will ask the user to clarify.
    if (
        classification.task is not None
        and not classification.task.owner_user_id
        and not classification.task.owner_display_name
        and slack_user_id
    ):
        classification.task.owner_user_id = slack_user_id
        classification.task.owner_display_name = f"<@{slack_user_id}>"
        classification.task.owner_assumed = True

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
