"""FR-CR-05-10 — Cross-channel team registry.

A single authoritative directory of «people we can assign tasks
to», unified across Slack and Telegram. Synced bidirectionally
with the `Team` tab of the spreadsheet pointed to by
`GOOGLE_TEAM_SHEETS_SPREADSHEET_ID`.

Revision ID: 0017_team_members
Revises: 0016_telegram_chat_members
Create Date: 2026-04-29 14:00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017_team_members"
down_revision: Union[str, None] = "0016_telegram_chat_members"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "team_members",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("real_name", sa.String(255), nullable=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_username", sa.String(64), nullable=True),
        sa.Column("slack_user_id", sa.String(64), nullable=True),
        sa.Column("role", sa.String(128), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column(
            "active", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("notes", sa.String(512), nullable=True),
        sa.Column(
            "last_synced_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("telegram_user_id", name="uq_team_members_tg_user_id"),
        sa.UniqueConstraint("slack_user_id", name="uq_team_members_slack_user_id"),
    )
    op.create_index(
        "ix_team_members_active", "team_members", ["active"]
    )
    op.create_index(
        "ix_team_members_telegram_username",
        "team_members",
        ["telegram_username"],
    )


def downgrade() -> None:
    op.drop_index("ix_team_members_telegram_username", "team_members")
    op.drop_index("ix_team_members_active", "team_members")
    op.drop_table("team_members")
