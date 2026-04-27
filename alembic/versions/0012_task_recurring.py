"""Recurring tasks (weekdays + start/end time).

Revision ID: 0012_task_recurring
Revises: 0011_daily_plan_items
Create Date: 2026-04-27 12:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_task_recurring"
down_revision: Union[str, None] = "0011_daily_plan_items"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column(
            "is_recurring",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "recurring_weekdays",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )
    op.add_column("tasks", sa.Column("recurring_start_time", sa.Time(), nullable=True))
    op.add_column("tasks", sa.Column("recurring_end_time", sa.Time(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "recurring_end_time")
    op.drop_column("tasks", "recurring_start_time")
    op.drop_column("tasks", "recurring_weekdays")
    op.drop_column("tasks", "is_recurring")
