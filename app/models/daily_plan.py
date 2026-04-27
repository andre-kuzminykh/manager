from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class DailyPlanItem(Base):
    """One row per (user, day, task) selected for that day's plan.

    Created by the evening cron (`plan-evening`) and consumed by the
    morning cron (`plan-morning`). The user's "Skip" button sets
    `excluded_at`; rows with `excluded_at IS NULL` make up the live
    plan.
    """

    __tablename__ = "daily_plan_items"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "plan_date", "task_id", name="uq_daily_plan_user_date_task"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_date: Mapped[date] = mapped_column(Date, nullable=False)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    excluded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
