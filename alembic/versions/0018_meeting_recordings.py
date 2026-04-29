"""FR-CR-05-39 — Fireflies meeting recordings registry.

One row per ingested Fireflies meeting. The pipeline writes /
updates the row through each step (audio download → Whisper →
detailed summary → Google Doc → short summary → task
extraction). Pipeline state flags let a re-poll resume mid-way
after a crash.

Also adds `'fireflies'` to the `task_source_kind` enum so tasks
extracted from a meeting get a discriminator distinct from
slack / telegram captures.

Revision ID: 0018_meeting_recordings
Revises: 0017_team_members
Create Date: 2026-04-29 21:00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018_meeting_recordings"
down_revision: Union[str, None] = "0017_team_members"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "meeting_recordings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("fireflies_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(512), nullable=True),
        sa.Column("meeting_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("participants", sa.JSON(), nullable=True),
        sa.Column("audio_url", sa.String(2048), nullable=True),
        sa.Column("fireflies_share_url", sa.String(2048), nullable=True),
        sa.Column("audio_path", sa.String(1024), nullable=True),
        sa.Column("transcript_text", sa.Text(), nullable=True),
        sa.Column("detailed_summary", sa.Text(), nullable=True),
        sa.Column("short_summary", sa.Text(), nullable=True),
        sa.Column("google_doc_id", sa.String(128), nullable=True),
        sa.Column("google_doc_url", sa.String(512), nullable=True),
        sa.Column("tasks_extracted_count", sa.Integer(), nullable=True),
        sa.Column(
            "audio_downloaded",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "transcribed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "detailed_summarised",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "short_summary_sent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "doc_exported",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "tasks_extracted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        sa.UniqueConstraint(
            "fireflies_id", name="uq_meeting_recordings_fireflies_id"
        ),
    )
    op.create_index(
        "ix_meeting_recordings_meeting_date",
        "meeting_recordings",
        ["meeting_date"],
    )

    # Extend the task_source_kind enum so extracted tasks have a
    # `'fireflies'` discriminator. Postgres needs an explicit
    # ALTER TYPE; SQLite (test backend) silently accepts any
    # string in an enum column, so we no-op there.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE task_source_kind ADD VALUE IF NOT EXISTS 'fireflies'")


def downgrade() -> None:
    op.drop_index(
        "ix_meeting_recordings_meeting_date", "meeting_recordings"
    )
    op.drop_table("meeting_recordings")
    # Postgres doesn't support removing enum values without a full
    # type rewrite; downgrade leaves the value present. Harmless
    # — no rows reference it once meeting_recordings is gone.
