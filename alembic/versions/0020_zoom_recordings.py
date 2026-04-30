"""FR-CR-05-116 — Zoom Cloud Recordings registry.

Mirrors `meeting_recordings` (FR-CR-05-39 / 0018) for Zoom as
a second meeting source. Same step-machine: audio_downloaded →
transcribed → detailed_summarised → doc_exported →
tasks_extracted.

Revision ID: 0020_zoom_recordings
Revises: 0019_team_members_notes_text
Create Date: 2026-04-30 15:30:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020_zoom_recordings"
down_revision: Union[str, None] = "0019_team_members_notes_text"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "zoom_recordings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("zoom_id", sa.String(128), nullable=False),
        sa.Column("zoom_meeting_id", sa.String(64), nullable=True),
        sa.Column("title", sa.String(512), nullable=True),
        sa.Column("meeting_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("participants", sa.JSON(), nullable=True),
        sa.Column("audio_url", sa.String(2048), nullable=True),
        sa.Column("zoom_share_url", sa.String(2048), nullable=True),
        sa.Column("audio_path", sa.String(1024), nullable=True),
        sa.Column("transcript_text", sa.Text(), nullable=True),
        sa.Column("detailed_summary", sa.Text(), nullable=True),
        sa.Column("short_summary", sa.Text(), nullable=True),
        sa.Column("google_doc_id", sa.String(128), nullable=True),
        sa.Column("google_doc_url", sa.String(512), nullable=True),
        sa.Column("tasks_extracted_count", sa.Integer(), nullable=True),
        sa.Column(
            "audio_downloaded", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "transcribed", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "detailed_summarised", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "short_summary_sent", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "doc_exported", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "tasks_extracted", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "attempts", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("zoom_id", name="uq_zoom_recordings_zoom_id"),
    )


def downgrade() -> None:
    op.drop_table("zoom_recordings")
