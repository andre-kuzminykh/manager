"""FR-CR-05-194 — slack_post_ts колонка для idempotent auto-publish.

Когда auto-Slack-send для встречи прошёл, ts храним → следующий
pipeline tick skip'нёт повторную публикацию.

Revision ID: 0034_slack_post_ts
Revises: 0033_entity_resolution_v2
Create Date: 2026-05-22
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0034_slack_post_ts"
down_revision = "0033_entity_resolution_v2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "zoom_recordings",
        sa.Column("slack_post_ts", sa.String(32), nullable=True),
    )
    op.add_column(
        "meeting_recordings",
        sa.Column("slack_post_ts", sa.String(32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("meeting_recordings", "slack_post_ts")
    op.drop_column("zoom_recordings", "slack_post_ts")
