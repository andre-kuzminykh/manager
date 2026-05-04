"""FR-CR-05-133 + FR-CR-05-138 — Telegram-side enrollment
widget for counterparty mentions that didn't resolve to the
directory.

FR-CR-05-138 supersedes the per-entity yes/no flow with a
single multi-select message:

  Stage 1 — multi-select grid (one `CounterpartyPromptBatch`
            per (recording, user)):
    «Found N unrecognised entities — tap numbers to select»
    [1][2][3][4][5]
    [6][7][8][9][10]
    [Next →]

  Stage 2 — for each selected, ask for canonical-name
            confirmation (text/voice or [Keep] / [Skip]).

  Stage 3 — for each kept name, ask for context
            (text/voice or [Skip]).

The legacy per-prompt yes/no statuses
(`pending_yesno`, `awaiting_context`) stay for back-compat
with existing rows; new flow uses `pending_selection`,
`pending_confirm_name`, `pending_context`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any  # noqa: F401  (kept for future JSON satellite)

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


# Status string constants — keep close to the model so callers
# can `from app.models.counterparty_prompt import STATUS_*`
# without circular imports.
# FR-CR-05-133 (legacy per-entity yes/no flow):
STATUS_PENDING_YESNO = "pending_yesno"
STATUS_AWAITING_CONTEXT = "awaiting_context"
STATUS_COMPLETED_ADDED = "completed_added"
STATUS_COMPLETED_SKIPPED = "completed_skipped"
STATUS_DECLINED = "declined"
# FR-CR-05-138 (batch multi-select flow):
STATUS_PENDING_SELECTION = "pending_selection"
STATUS_PENDING_CONFIRM_NAME = "pending_confirm_name"
STATUS_PENDING_CONTEXT = "pending_context"
STATUS_BATCH_PROCESSING = "processing"
STATUS_BATCH_COMPLETED = "completed"

# Per-batch step pointer (selected entities iterate one at a
# time through these two sub-stages).
STEP_CONFIRM_NAME = "confirm_name"
STEP_CONTEXT = "context"


class CounterpartyPrompt(Base, TimestampMixin):
    """One enrollment row per unresolved counterparty mention,
    tied to a recipient. May be part of a batch (FR-CR-05-138)
    or standalone (legacy FR-CR-05-133)."""

    __tablename__ = "counterparty_prompts"
    __table_args__ = (
        UniqueConstraint(
            "source_kind", "source_id",
            "mention_normalised", "user_id",
            name="uq_counterparty_prompts_per_recipient",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )

    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # Surface form as Pass 1 extracted it («Тезер», «Bauer/Dart»).
    mention_text: Mapped[str] = mapped_column(String(512), nullable=False)
    # Folded form so re-runs dedupe regardless of Whisper's
    # casing differences.
    mention_normalised: Mapped[str] = mapped_column(
        String(512), nullable=False, index=True
    )

    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Telegram message_ids for in-place edits. Nullable because
    # Telegram send may fail and we still want the row for audit.
    yesno_message_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    context_message_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=STATUS_PENDING_YESNO
    )

    # Operator's free-text reply (or Whisper-transcribed voice).
    context_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_counterparty_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("counterparties.id", ondelete="SET NULL"),
        nullable=True,
    )

    responded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # FR-CR-05-138 — batch multi-select flow.
    batch_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("counterparty_prompt_batches.id", ondelete="SET NULL"),
        nullable=True,
    )
    index_in_batch: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    selected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    canonical_name_corrected: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )

    counterparty = relationship("Counterparty")
    batch = relationship(
        "CounterpartyPromptBatch", back_populates="prompts"
    )


class CounterpartyPromptBatch(Base, TimestampMixin):
    """FR-CR-05-138 — one batch per (recording, user). Carries
    the multi-select message id + processing pointer."""

    __tablename__ = "counterparty_prompt_batches"
    __table_args__ = (
        UniqueConstraint(
            "source_kind", "source_id", "user_id",
            name="uq_counterparty_prompt_batches_per_user",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )

    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)

    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    multiselect_message_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )

    entity_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=STATUS_PENDING_SELECTION
    )
    # 1-based index into the SELECTED subset of prompts.
    current_index: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    # Which sub-step we're on for current_index:
    # `confirm_name` or `context`. Null when not processing.
    current_step: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )

    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    prompts = relationship(
        "CounterpartyPrompt", back_populates="batch",
        order_by="CounterpartyPrompt.index_in_batch",
    )


__all__ = [
    "CounterpartyPrompt",
    "CounterpartyPromptBatch",
    "STATUS_PENDING_YESNO",
    "STATUS_AWAITING_CONTEXT",
    "STATUS_COMPLETED_ADDED",
    "STATUS_COMPLETED_SKIPPED",
    "STATUS_DECLINED",
    "STATUS_PENDING_SELECTION",
    "STATUS_PENDING_CONFIRM_NAME",
    "STATUS_PENDING_CONTEXT",
    "STATUS_BATCH_PROCESSING",
    "STATUS_BATCH_COMPLETED",
    "STEP_CONFIRM_NAME",
    "STEP_CONTEXT",
]
