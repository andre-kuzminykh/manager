"""FR-CR-05-07 — Telegram chat-members registry.

Per-chat membership so the classifier can resolve mentions like
«Валя сделай X» against a real Telegram user_id and the bot can
DM the assignee directly (when they've /started the bot). The
listener upserts a row every time it sees a fresh message in a
chat, so the registry self-populates from live traffic.

Revision ID: 0016_telegram_chat_members
Revises: 0015_telegram_listener_state
Create Date: 2026-04-29 12:30:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016_telegram_chat_members"
down_revision: Union[str, None] = "0015_telegram_listener_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "telegram_chat_members",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("first_name", sa.String(128), nullable=True),
        sa.Column("last_name", sa.String(128), nullable=True),
        sa.Column("has_started_bot", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
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
        sa.PrimaryKeyConstraint("chat_id", "user_id"),
    )
    op.create_index(
        "ix_telegram_chat_members_username",
        "telegram_chat_members",
        ["username"],
    )
    op.create_index(
        "ix_telegram_chat_members_user_id",
        "telegram_chat_members",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_telegram_chat_members_user_id", "telegram_chat_members")
    op.drop_index("ix_telegram_chat_members_username", "telegram_chat_members")
    op.drop_table("telegram_chat_members")
