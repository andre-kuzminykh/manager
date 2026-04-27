"""Optional due_time on tasks (filled by Edit modal).

Revision ID: 0009_task_due_time
Revises: 0008_archive_and_transcript
Create Date: 2026-04-27 07:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_task_due_time"
down_revision: Union[str, None] = "0008_archive_and_transcript"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("due_time", sa.Time(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "due_time")
