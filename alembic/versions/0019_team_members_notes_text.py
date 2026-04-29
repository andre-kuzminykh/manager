"""FR-CR-05-42 — widen `team_members.notes` to TEXT.

Operator pasted multi-paragraph notes (>512 chars) into the
Team sheet, which made every Sheet → DB pull fail with
`StringDataRightTruncation`. The 200-char cap on the
owner-prompt block (FR-CR-05-31) keeps prompts reasonable
even with a very long DB value, so widening the column is
safe.

SQLite is no-op friendly: ALTER COLUMN TYPE doesn't fully
work on SQLite ≤3.35, but `op.alter_column` with
`existing_type=String(512), type_=Text()` is a metadata-only
change there since both map to TEXT internally. PostgreSQL
issues a real `ALTER TYPE`.

Revision ID: 0019_team_members_notes_text
Revises: 0018_meeting_recordings
Create Date: 2026-04-29 22:30:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019_team_members_notes_text"
down_revision: Union[str, None] = "0018_meeting_recordings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.alter_column(
            "team_members",
            "notes",
            existing_type=sa.String(512),
            type_=sa.Text(),
            existing_nullable=True,
        )
    # SQLite (test DB) has TEXT-equivalent VARCHAR already; no-op.


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.alter_column(
            "team_members",
            "notes",
            existing_type=sa.Text(),
            type_=sa.String(512),
            existing_nullable=True,
        )
