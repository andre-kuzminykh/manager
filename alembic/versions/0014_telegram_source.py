"""FR-CR-04-26 — multi-channel task sources (Slack + Telegram).

Two related schema changes:

1. ``tasks.source_kind`` (ENUM 'slack' | 'telegram') — discriminator
   for channel-aware queries / sheet-side rendering / UI routing.
   Existing rows are backfilled to 'slack'.

2. ``processed_telegram_messages`` — bookkeeping table for the
   Telegram ingest worker. Telegram messages live in a read-only
   Supabase view (``humanoid_tg_chats_readonly``); our local DB
   never copies them. We just record (chat_id, message_id) of every
   message we've already fed through the intent pipeline so the
   ingest cron is idempotent and can resume from any point.

Revision ID: 0014_telegram_source
Revises: 0013_soft_delete_drop_review
Create Date: 2026-04-28 12:00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_telegram_source"
down_revision: Union[str, None] = "0013_soft_delete_drop_review"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    # 1) tasks.source_kind ----------------------------------------------------
    if bind.dialect.name == "postgresql":
        # Create the enum type explicitly so we can ALTER it later.
        op.execute("CREATE TYPE task_source_kind AS ENUM ('slack', 'telegram')")
        op.add_column(
            "tasks",
            sa.Column(
                "source_kind",
                sa.Enum("slack", "telegram", name="task_source_kind", create_type=False),
                nullable=False,
                server_default="slack",
            ),
        )
    else:
        # SQLite: enums are plain strings.
        op.add_column(
            "tasks",
            sa.Column(
                "source_kind",
                sa.Enum("slack", "telegram", name="task_source_kind"),
                nullable=False,
                server_default="slack",
            ),
        )

    # 2) processed_telegram_messages ---------------------------------------
    op.create_table(
        "processed_telegram_messages",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("chat_id", "message_id"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_processed_telegram_messages_processed_at",
        "processed_telegram_messages",
        ["processed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_processed_telegram_messages_processed_at",
        table_name="processed_telegram_messages",
    )
    op.drop_table("processed_telegram_messages")
    op.drop_column("tasks", "source_kind")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TYPE task_source_kind")
