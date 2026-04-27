"""Start date/time, category, parent_task_id (subtasks).

Revision ID: 0010_task_extras
Revises: 0009_task_due_time
Create Date: 2026-04-27 09:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_task_extras"
down_revision: Union[str, None] = "0009_task_due_time"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("start_date", sa.Date(), nullable=True))
    op.add_column("tasks", sa.Column("start_time", sa.Time(), nullable=True))
    op.add_column("tasks", sa.Column("category", sa.String(64), nullable=True))
    op.add_column(
        "tasks",
        sa.Column(
            "parent_task_id",
            sa.Integer(),
            sa.ForeignKey("tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_tasks_parent_task_id", "tasks", ["parent_task_id"]
    )
    op.create_index("ix_tasks_category", "tasks", ["category"])


def downgrade() -> None:
    op.drop_index("ix_tasks_category", table_name="tasks")
    op.drop_index("ix_tasks_parent_task_id", table_name="tasks")
    op.drop_column("tasks", "parent_task_id")
    op.drop_column("tasks", "category")
    op.drop_column("tasks", "start_time")
    op.drop_column("tasks", "start_date")
