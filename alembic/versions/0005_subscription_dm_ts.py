"""Anchor DM ts on task_subscriptions so broadcasts thread under it.

Revision ID: 0005_subscription_dm_ts
Revises: 0004_widget_log
Create Date: 2026-04-23 23:30:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_subscription_dm_ts"
down_revision: Union[str, None] = "0004_widget_log"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "task_subscriptions",
        sa.Column("dm_ts", sa.String(32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("task_subscriptions", "dm_ts")
