"""CR-01 amendments: lifecycle fields, status history, subscriptions.

Revision ID: 0002_cr_amendments
Revises: 0001_initial
Create Date: 2026-04-23 12:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_cr_amendments"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Task lifecycle columns -------------------------------------------
    op.add_column(
        "tasks",
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tasks", sa.Column("estimated_minutes", sa.Integer, nullable=True)
    )
    op.add_column(
        "tasks",
        sa.Column(
            "is_current_week",
            sa.Boolean,
            nullable=False,
            server_default=sa.true(),
        ),
    )
    # Drop the server_default after backfill so the ORM owns the value.
    op.alter_column("tasks", "is_current_week", server_default=None)

    task_status = sa.Enum(
        "backlog",
        "todo",
        "in_progress",
        "review",
        "done",
        name="task_status",
        create_type=False,
    )

    # --- task_status_history ---------------------------------------------
    op.create_table(
        "task_status_history",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "task_id",
            sa.Integer,
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("from_status", task_status, nullable=True),
        sa.Column("to_status", task_status, nullable=False),
        sa.Column("changed_by_slack_user_id", sa.String(64), nullable=True),
        sa.Column("reason", sa.String(255), nullable=True),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # --- task_subscriptions ----------------------------------------------
    op.create_table(
        "task_subscriptions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "task_id",
            sa.Integer,
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("slack_user_id", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "task_id", "slack_user_id", name="uq_task_subscriptions_task_user"
        ),
    )


def downgrade() -> None:
    op.drop_table("task_subscriptions")
    op.drop_table("task_status_history")
    op.drop_column("tasks", "is_current_week")
    op.drop_column("tasks", "estimated_minutes")
    op.drop_column("tasks", "completed_at")
    op.drop_column("tasks", "started_at")
