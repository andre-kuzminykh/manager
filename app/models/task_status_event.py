"""FR-ST-LOG — unified append-only log of every task field change.

Additive satellite of ``tasks`` (SPEC_STATUS_TRACKER_v0.2 §2). Every
status / due / owner / comment update — from chat (Task Vector P6),
meetings (Status Tracker), a manual sheet edit, or a rollback — writes
exactly ONE row here with ``from_value`` -> ``to_value`` + ``source`` +
``actor``. This is the single source of truth for «что обновлялось» and
for rollback (§3).

It does NOT replace ``TaskStatusHistory`` (enum-only transition audit) —
this is the cross-source superset (any field, any source, comment-only).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TaskStatusEvent(Base):
    __tablename__ = "task_status_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # chat | zoom | fireflies | sheet | rollback
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # status | due_date | owner | comment
    field: Mapped[str] = mapped_column(String(16), nullable=False)
    from_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # meeting provenance — split out for the idempotency constraint below
    meeting_source_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    meeting_segment_idx: Mapped[int | None] = mapped_column(Integer, nullable=True)
    meeting_ref: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # True = applied to the task; False = logged for review (ambiguous match)
    applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # FR-ST-LOG-2 — meeting idempotency. Chat/manual events leave the
    # meeting_* columns NULL, and NULLs are distinct in a UNIQUE index
    # (both Postgres and SQLite), so they never collide; a replayed
    # meeting event (same source/source_id/segment/task) is rejected.
    __table_args__ = (
        UniqueConstraint(
            "source",
            "meeting_source_id",
            "meeting_segment_idx",
            "task_id",
            name="uq_task_status_events_meeting",
        ),
    )


__all__ = ["TaskStatusEvent"]
