import enum
from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Date, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class TaskPriority(str, enum.Enum):
    low = "low"
    medium = "medium"
    high = "high"
    urgent = "urgent"


class TaskStatus(str, enum.Enum):
    open = "open"
    in_progress = "in_progress"
    done = "done"
    cancelled = "cancelled"


class MeetingStatus(str, enum.Enum):
    scheduled = "scheduled"
    cancelled = "cancelled"
    done = "done"


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner_display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    priority: Mapped[TaskPriority] = mapped_column(
        Enum(TaskPriority, name="task_priority"), nullable=False, default=TaskPriority.medium
    )
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name="task_status"), nullable=False, default=TaskStatus.open
    )

    # source linkage
    source_conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_message_ts: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_thread_ts: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_permalink: Mapped[str | None] = mapped_column(String(512), nullable=True)
    context_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("context_snapshots.id"), nullable=True
    )
    created_by_slack_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # external sync
    google_sheets_row_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    google_tasks_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    extra: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    sheets_sync: Mapped["GoogleSheetsSync | None"] = relationship(  # noqa: F821
        back_populates="task", uselist=False, cascade="all, delete-orphan"
    )
    tasks_sync: Mapped["GoogleTasksSync | None"] = relationship(  # noqa: F821
        back_populates="task", uselist=False, cascade="all, delete-orphan"
    )


class Meeting(Base, TimestampMixin):
    __tablename__ = "meetings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    participants: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    datetime_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[MeetingStatus] = mapped_column(
        Enum(MeetingStatus, name="meeting_status"),
        nullable=False,
        default=MeetingStatus.scheduled,
    )

    source_conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_message_ts: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_thread_ts: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_permalink: Mapped[str | None] = mapped_column(String(512), nullable=True)
    context_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("context_snapshots.id"), nullable=True
    )
    created_by_slack_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    extra: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
