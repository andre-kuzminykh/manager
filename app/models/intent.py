import enum
from typing import Any

from sqlalchemy import JSON, Enum, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class ActionDraftState(str, enum.Enum):
    proposed = "proposed"
    confirmed = "confirmed"
    edited = "edited"
    ignored = "ignored"
    expired = "expired"
    failed = "failed"


class IntentType(str, enum.Enum):
    create_task = "create_task"
    create_meeting = "create_meeting"
    update_task = "update_task"
    update_meeting = "update_meeting"
    no_action = "no_action"


class IntentInference(Base, TimestampMixin):
    __tablename__ = "intent_inferences"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    context_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("context_snapshots.id"), nullable=True
    )
    intent: Mapped[IntentType] = mapped_column(
        Enum(IntentType, name="intent_type"), nullable=False
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    invocation_type: Mapped[str] = mapped_column(String(32), nullable=False)  # passive/mention/shortcut
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)

    drafts: Mapped[list["ActionDraft"]] = relationship(back_populates="inference")


class ActionDraft(Base, TimestampMixin):
    __tablename__ = "action_drafts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    inference_id: Mapped[int] = mapped_column(
        ForeignKey("intent_inferences.id"), nullable=False
    )
    intent: Mapped[IntentType] = mapped_column(
        Enum(IntentType, name="intent_type"), nullable=False
    )
    state: Mapped[ActionDraftState] = mapped_column(
        Enum(ActionDraftState, name="action_draft_state"),
        nullable=False,
        default=ActionDraftState.proposed,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_by_slack_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    slack_message_ts: Mapped[str | None] = mapped_column(String(32), nullable=True)

    inference: Mapped[IntentInference] = relationship(back_populates="drafts")
