"""FR-CR-05-133 — `counterparty_prompts` table for the
in-Telegram enrollment widget that asks the operator about each
unresolved counterparty mention from a meeting.

One row per (recording, mention, recipient). Status walks the
state machine:

  pending_yesno → awaiting_context → completed_added
                 → declined          → completed_skipped

`UNIQUE(source_kind, source_id, mention_normalised, user_id)`
makes the pipeline rerun-idempotent: re-emitting the meeting
won't double-post the widget to the same admin.

Revision ID: 0025_counterparty_prompts
Revises: 0024_counterparties_drop_type
Create Date: 2026-05-02 13:30:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025_counterparty_prompts"
down_revision: Union[str, None] = "0024_counterparties_drop_type"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "counterparty_prompts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(128), nullable=False),
        sa.Column("mention_text", sa.String(512), nullable=False),
        sa.Column("mention_normalised", sa.String(512), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("yesno_message_id", sa.BigInteger(), nullable=True),
        sa.Column("context_message_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "status", sa.String(32), nullable=False,
            server_default="pending_yesno",
        ),
        sa.Column("context_text", sa.Text(), nullable=True),
        sa.Column("created_counterparty_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "responded_at", sa.DateTime(timezone=True), nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["created_counterparty_id"], ["counterparties.id"],
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "source_kind", "source_id",
            "mention_normalised", "user_id",
            name="uq_counterparty_prompts_per_recipient",
        ),
    )
    op.create_index(
        "ix_counterparty_prompts_mention_normalised",
        "counterparty_prompts",
        ["mention_normalised"],
    )
    op.create_index(
        "ix_counterparty_prompts_status",
        "counterparty_prompts",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_counterparty_prompts_status", table_name="counterparty_prompts"
    )
    op.drop_index(
        "ix_counterparty_prompts_mention_normalised",
        table_name="counterparty_prompts",
    )
    op.drop_table("counterparty_prompts")
