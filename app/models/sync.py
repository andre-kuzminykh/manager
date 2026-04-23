import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class SyncStatus(str, enum.Enum):
    pending = "pending"
    success = "success"
    failed = "failed"


class GoogleSheetsSync(Base, TimestampMixin):
    __tablename__ = "google_sheets_sync"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    spreadsheet_id: Mapped[str] = mapped_column(String(128), nullable=False)
    row_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[SyncStatus] = mapped_column(
        Enum(SyncStatus, name="sync_status"), nullable=False, default=SyncStatus.pending
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    task: Mapped["Task"] = relationship(back_populates="sheets_sync")  # noqa: F821


class GoogleTasksSync(Base, TimestampMixin):
    __tablename__ = "google_tasks_sync"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    google_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tasklist_id: Mapped[str] = mapped_column(String(128), nullable=False)
    google_task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[SyncStatus] = mapped_column(
        Enum(SyncStatus, name="sync_status", create_type=False),
        nullable=False,
        default=SyncStatus.pending,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    task: Mapped["Task"] = relationship(back_populates="tasks_sync")  # noqa: F821
