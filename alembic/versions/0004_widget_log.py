"""Widget-morph: store card ts on Task, follow-up ts on ActionDraft.

Revision ID: 0004_widget_log
Revises: 0003_draft_followup
Create Date: 2026-04-23 23:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_widget_log"
down_revision: Union[str, None] = "0003_draft_followup"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # In-place updates for the task card (channel + DM mirror).
    op.add_column("tasks", sa.Column("card_channel", sa.String(64), nullable=True))
    op.add_column("tasks", sa.Column("card_ts", sa.String(32), nullable=True))
    op.add_column("tasks", sa.Column("dm_channel", sa.String(64), nullable=True))
    op.add_column("tasks", sa.Column("dm_ts", sa.String(32), nullable=True))

    # List of ts values for bot's follow-up questions/acks so we can delete
    # them on confirm/ignore.
    op.add_column(
        "action_drafts",
        sa.Column("follow_up_message_ts", sa.JSON, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("action_drafts", "follow_up_message_ts")
    op.drop_column("tasks", "dm_ts")
    op.drop_column("tasks", "dm_channel")
    op.drop_column("tasks", "card_ts")
    op.drop_column("tasks", "card_channel")
