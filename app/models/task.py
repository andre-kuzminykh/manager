import enum
from datetime import date, datetime, time
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class TaskPriority(str, enum.Enum):
    low = "low"
    medium = "medium"
    high = "high"
    urgent = "urgent"


class TaskStatus(str, enum.Enum):
    """Lifecycle is backlog → todo → in_progress → done.

    The `review` value was retired in FR-CR-04-20 (migration 0013).
    Soft-deleted tasks aren't a status — they keep their last status
    plus a non-null `deleted_at` timestamp.
    """

    backlog = "backlog"
    todo = "todo"
    in_progress = "in_progress"
    done = "done"


class TaskSourceKind(str, enum.Enum):
    """FR-CR-04-26 / FR-CR-05-39 — discriminator for the channel
    a task came from.

    For Slack the source_* fields hold Slack identifiers, for
    Telegram the chat id / message id / reply-to id / a t.me
    link, for Fireflies the meeting transcript id /
    transcript-locator / participants list / a Fireflies share
    URL.
    """

    slack = "slack"
    telegram = "telegram"
    fireflies = "fireflies"
    # FR-CR-05-116 — Zoom Cloud Recordings as a second meeting
    # source (mirror of Fireflies). Tasks extracted from a
    # Zoom transcript carry source_kind=zoom + source_permalink
    # set to the zoom share_url when available.
    zoom = "zoom"


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
    # Optional clock time on the deadline. Filled when the user enters
    # it in the Edit modal — pipeline never sets it. Combined with
    # due_date it gives a full datetime; without due_date the time is
    # ignored on display.
    due_time: Mapped[time | None] = mapped_column(Time, nullable=True)

    # When the assignee plans to start. Pure data fields right now;
    # they'll feed a future calendar-booking integration but no UI
    # depends on them yet.
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    start_time: Mapped[time | None] = mapped_column(Time, nullable=True)

    # Free-form direction / department label ("маркетинг", "разработка",
    # "ops", …). String so adding a new category never needs a
    # migration. Drop-down list lives in CATEGORIES env config.
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Subtask hierarchy: one parent per child task, no enforced depth
    # limit. ondelete=SET NULL so deleting a parent leaves the child
    # rows alive (orphaned, but not lost).
    parent_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )

    # Recurring schedule. is_recurring is the master switch (UI
    # checkbox); the three other columns describe the recurrence —
    # which weekdays, and the time range of one occurrence. They are
    # ignored at runtime when is_recurring is False.
    is_recurring: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    recurring_weekdays: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    recurring_start_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    recurring_end_time: Mapped[time | None] = mapped_column(Time, nullable=True)

    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name="task_status"), nullable=False, default=TaskStatus.backlog
    )

    # CR-01: lifecycle additions
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    estimated_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_current_week: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # FR-CR-04-20: soft delete. When non-null the task is hidden from UI,
    # digests, plans, workload — but preserved in audit logs.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    # FR-CR-04-26: which channel did this task come from?
    source_kind: Mapped[TaskSourceKind] = mapped_column(
        Enum(TaskSourceKind, name="task_source_kind"),
        nullable=False,
        default=TaskSourceKind.slack,
        server_default="slack",
    )

    # source linkage — fields are channel-agnostic.
    # Slack: conversation_id = channel id, message_ts / thread_ts as ts strings.
    # Telegram: conversation_id = chat id, message_ts = message_id, thread_ts = reply_to_message_id.
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

    # in-place Slack card coordinates: one in the source channel, one in a
    # DM with the task's owner so status changes can chat.update both.
    card_channel: Mapped[str | None] = mapped_column(String(64), nullable=True)
    card_ts: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dm_channel: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dm_ts: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # CR-03: evidence attached on completion.
    completion_artifact: Mapped[str | None] = mapped_column(Text, nullable=True)
    completion_artifact_kind: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )

    extra: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    sheets_sync: Mapped["GoogleSheetsSync | None"] = relationship(  # noqa: F821
        back_populates="task", uselist=False, cascade="all, delete-orphan"
    )
    tasks_sync: Mapped["GoogleTasksSync | None"] = relationship(  # noqa: F821
        back_populates="task", uselist=False, cascade="all, delete-orphan"
    )
    history: Mapped[list["TaskStatusHistory"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="TaskStatusHistory.at",
    )
    subscriptions: Mapped[list["TaskSubscription"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )

    # Self-referencing parent/children for subtasks.
    parent: Mapped["Task | None"] = relationship(
        "Task", remote_side="Task.id", back_populates="subtasks"
    )
    subtasks: Mapped[list["Task"]] = relationship(
        "Task", back_populates="parent",
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


class TaskStatusHistory(Base):
    """Audit trail of task status transitions (CR-01)."""

    __tablename__ = "task_status_history"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_status: Mapped[TaskStatus | None] = mapped_column(
        Enum(TaskStatus, name="task_status", create_type=False), nullable=True
    )
    to_status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name="task_status", create_type=False), nullable=False
    )
    changed_by_slack_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )

    task: Mapped["Task"] = relationship(back_populates="history")


class TaskSubscription(Base, TimestampMixin):
    """Slack user subscribed to updates on a task (CR-01)."""

    __tablename__ = "task_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    slack_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # ts of the anchor DM the bot posted for this (task, user) pair.
    # Later broadcasts thread under it so all updates about one task land
    # in a single DM conversation.
    dm_ts: Mapped[str | None] = mapped_column(String(32), nullable=True)

    task: Mapped["Task"] = relationship(back_populates="subscriptions")

    __table_args__ = ()


from sqlalchemy import UniqueConstraint  # noqa: E402

TaskSubscription.__table_args__ = (
    UniqueConstraint("task_id", "slack_user_id", name="uq_task_subscriptions_task_user"),
)
