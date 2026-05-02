"""FR-CR-05-133 — Telegram-side enrollment widget for counter-
party mentions that didn't resolve to the directory.

After Pass 2 (`resolve_mentions_to_directory`) finishes for a
meeting, the pipeline lists every mention with
`directory_id is None` (Whisper heard a name the operator never
added to Sheets). For each unresolved mention × each admin
recipient, the bot posts a two-stage widget:

  Stage 1: «Track «<name>»? [Yes] [No]»
  Stage 2 (after Yes): «Send text or voice context, or [Skip].»

`CounterpartyPrompt` rows persist that flow durably so:
  - the operator can answer hours later (after the morning DM
    summary lands at start of workday) without state loss across
    listener restarts (the in-memory `PendingRegistry` is for
    short-lived flows only — FR-CR-04-29 docstring);
  - `(source_kind, source_id, mention_normalised, user_id)` is
    UNIQUE so re-running a meeting pipeline doesn't double-post.

Status transitions:

    pending_yesno ─[Yes]──→ awaiting_context ─[text/voice]→ completed_added
                  ─[No]───→ declined          ─[Skip]──────→ completed_skipped

Both terminal-completed paths optionally write a `Counterparty`
hub (and a `CounterpartyAttribute` satellite carrying the
operator's notes when a context reply arrived) — captured via
`created_counterparty_id` for audit.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any  # noqa: F401  (kept for future JSON satellite)

from sqlalchemy import (
    BigInteger,
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
STATUS_PENDING_YESNO = "pending_yesno"
STATUS_AWAITING_CONTEXT = "awaiting_context"
STATUS_COMPLETED_ADDED = "completed_added"
STATUS_COMPLETED_SKIPPED = "completed_skipped"
STATUS_DECLINED = "declined"


class CounterpartyPrompt(Base, TimestampMixin):
    """One enrollment widget for one unresolved counterparty
    mention, delivered to one Telegram user."""

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

    counterparty = relationship("Counterparty")


__all__ = [
    "CounterpartyPrompt",
    "STATUS_PENDING_YESNO",
    "STATUS_AWAITING_CONTEXT",
    "STATUS_COMPLETED_ADDED",
    "STATUS_COMPLETED_SKIPPED",
    "STATUS_DECLINED",
]
