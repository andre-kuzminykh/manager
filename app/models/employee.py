from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Employee(Base, TimestampMixin):
    """Slack team-member directory row.

    Populated automatically from Slack ``users.info`` the first time we see
    a user post (or react, reply, etc.). Refreshed at most once per
    ``EMPLOYEE_REFRESH_TTL_SECONDS`` to stay inside Slack rate limits.
    """

    __tablename__ = "employees"

    slack_user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    team_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    real_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)

    is_bot: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    profile_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    profile_raw: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
