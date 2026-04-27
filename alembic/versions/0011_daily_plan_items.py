"""Daily plan: evening approval + morning execution list.

Revision ID: 0011_daily_plan_items
Revises: 0010_task_extras
Create Date: 2026-04-27 11:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_daily_plan_items"
down_revision: Union[str, None] = "0010_task_extras"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "daily_plan_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("plan_date", sa.Date(), nullable=False),
        sa.Column(
            "task_id",
            sa.Integer(),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("excluded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "user_id", "plan_date", "task_id", name="uq_daily_plan_user_date_task"
        ),
    )
    op.create_index(
        "ix_daily_plan_user_date", "daily_plan_items", ["user_id", "plan_date"]
    )


def downgrade() -> None:
    op.drop_index("ix_daily_plan_user_date", table_name="daily_plan_items")
    op.drop_table("daily_plan_items")
