"""Link ActionDraft to the Task it created (for always-create flows).

Revision ID: 0007_draft_task_link
Revises: 0006_employees_and_admin
Create Date: 2026-04-24 12:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_draft_task_link"
down_revision: Union[str, None] = "0006_employees_and_admin"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "action_drafts",
        sa.Column(
            "task_id",
            sa.Integer,
            sa.ForeignKey("tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_action_drafts_task_id", "action_drafts", ["task_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_action_drafts_task_id", "action_drafts")
    op.drop_column("action_drafts", "task_id")
