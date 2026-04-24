"""Employees directory + completion artifact fields.

Revision ID: 0006_employees_and_admin
Revises: 0005_subscription_dm_ts
Create Date: 2026-04-24 00:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_employees_and_admin"
down_revision: Union[str, None] = "0005_subscription_dm_ts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "employees",
        sa.Column("slack_user_id", sa.String(64), primary_key=True),
        sa.Column("team_id", sa.String(64), nullable=True),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("real_name", sa.String(255), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("title", sa.String(255), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=True),
        sa.Column(
            "is_bot",
            sa.Boolean,
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "is_admin",
            sa.Boolean,
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "profile_refreshed_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("profile_raw", sa.JSON, nullable=True),
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
    )
    # Strip the server defaults now that the rows exist with boolean seeds.
    op.alter_column("employees", "is_bot", server_default=None)
    op.alter_column("employees", "is_admin", server_default=None)

    op.create_index(
        "ix_slack_messages_user_id", "slack_messages", ["user_id"]
    )

    # Task completion artifact fields.
    op.add_column("tasks", sa.Column("completion_artifact", sa.Text, nullable=True))
    op.add_column(
        "tasks",
        sa.Column(
            "completion_artifact_kind", sa.String(16), nullable=True
        ),
    )


def downgrade() -> None:
    op.drop_column("tasks", "completion_artifact_kind")
    op.drop_column("tasks", "completion_artifact")
    op.drop_index("ix_slack_messages_user_id", "slack_messages")
    op.drop_table("employees")
