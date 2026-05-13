"""FR-CR-05-165 — Pre-meeting agenda hub.

One row per Calendar event that the agenda runner has already
posted to Slack. Acts as the idempotency key so a runner tick that
fires twice within the lead-time window can't double-DM.

See migration 0027_meeting_agendas for column rationale.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class MeetingAgenda(Base, TimestampMixin):
    __tablename__ = "meeting_agendas"
    __table_args__ = (
        UniqueConstraint(
            "calendar_event_id",
            name="uq_meeting_agendas_calendar_event_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    calendar_event_id: Mapped[str] = mapped_column(String(256), nullable=False)
    recurring_event_id: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    title_normalised: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )
    scheduled_start_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    posted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    slack_channel: Mapped[str] = mapped_column(String(64), nullable=False)
    slack_ts: Mapped[str | None] = mapped_column(String(64), nullable=True)
    google_doc_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    google_doc_url: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )
    prior_meeting_zoom_ids: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True
    )
