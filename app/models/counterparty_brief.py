"""FR-CR-05-168 — Counterparty Briefs models.

Two tables:
  * `CounterpartyBrief`        — per-counterparty Doc cache.
  * `CounterpartyBriefsEvent`  — per-event idempotency.
  * `CounterpartyBriefLink`    — N-to-N event ↔ brief mapping.

See migration `0030_counterparty_briefs.py` and
SPEC_COUNTERPARTY_BRIEFS_v0.1.md for column rationale.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class CounterpartyBrief(Base, TimestampMixin):
    __tablename__ = "counterparty_briefs"
    __table_args__ = (
        UniqueConstraint(
            "counterparty_key",
            name="uq_counterparty_briefs_counterparty_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    counterparty_key: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # 'org'|'person'
    display_name: Mapped[str] = mapped_column(String(512), nullable=False)
    org_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    counterparty_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    research_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, default=Decimal("0")
    )
    google_doc_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    google_doc_url: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )
    researched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class CounterpartyBriefsEvent(Base, TimestampMixin):
    __tablename__ = "counterparty_briefs_events"
    __table_args__ = (
        UniqueConstraint(
            "calendar_event_id",
            name="uq_counterparty_briefs_events_calendar_event_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    calendar_event_id: Mapped[str] = mapped_column(String(256), nullable=False)
    event_title: Mapped[str] = mapped_column(String(512), nullable=False)
    scheduled_meeting_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    posted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    slack_channel: Mapped[str] = mapped_column(String(64), nullable=False)
    slack_ts: Mapped[str | None] = mapped_column(String(64), nullable=True)
    total_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, default=Decimal("0")
    )
    link_summary: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON, nullable=True
    )


class CounterpartyBriefLink(Base):
    __tablename__ = "counterparty_brief_links"
    __table_args__ = (
        UniqueConstraint(
            "event_id", "brief_id",
            name="uq_counterparty_brief_links_event_brief",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("counterparty_briefs_events.id", ondelete="CASCADE"),
        nullable=False,
    )
    brief_id: Mapped[int] = mapped_column(
        ForeignKey("counterparty_briefs.id", ondelete="CASCADE"),
        nullable=False,
    )
