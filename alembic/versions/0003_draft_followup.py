"""Follow-up state on action_drafts (card ts, awaiting_field).

Revision ID: 0003_draft_followup
Revises: 0002_cr_amendments
Create Date: 2026-04-23 22:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_draft_followup"
down_revision: Union[str, None] = "0002_cr_amendments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("action_drafts", sa.Column("card_channel", sa.String(64), nullable=True))
    op.add_column("action_drafts", sa.Column("card_ts", sa.String(32), nullable=True))
    op.add_column("action_drafts", sa.Column("awaiting_field", sa.String(32), nullable=True))
    op.create_index(
        "ix_action_drafts_thread",
        "action_drafts",
        ["slack_message_ts"],
    )


def downgrade() -> None:
    op.drop_index("ix_action_drafts_thread", "action_drafts")
    op.drop_column("action_drafts", "awaiting_field")
    op.drop_column("action_drafts", "card_ts")
    op.drop_column("action_drafts", "card_channel")
