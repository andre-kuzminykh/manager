from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import ActionDraft, ActionDraftState, Meeting


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def create_meeting_from_draft(
    session: Session,
    *,
    draft: ActionDraft,
    source: dict[str, Any],
    context_snapshot_id: int | None,
    fallback_author_slack_id: str | None,
) -> Meeting:
    payload: dict[str, Any] = draft.payload or {}
    title = (payload.get("title") or "").strip()
    if not title:
        raise ValueError("Meeting title is required")

    participants = payload.get("participants") or []
    if isinstance(participants, str):
        participants = [p.strip() for p in participants.split(",") if p.strip()]

    meeting = Meeting(
        title=title,
        notes=payload.get("notes"),
        participants=list(participants),
        datetime_at=_coerce_datetime(payload.get("datetime_at")),
        timezone=payload.get("timezone"),
        source_conversation_id=source.get("conversation_id"),
        source_message_ts=source.get("message_ts"),
        source_thread_ts=source.get("thread_ts"),
        source_permalink=source.get("permalink"),
        context_snapshot_id=context_snapshot_id,
        created_by_slack_user_id=draft.created_by_slack_user_id or fallback_author_slack_id,
    )
    session.add(meeting)
    draft.state = ActionDraftState.confirmed
    session.flush()
    return meeting
