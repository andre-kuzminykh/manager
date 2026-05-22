"""FR-CR-05-193 — entity_resolution_cache + ZoomRecording/MeetingRecording
`extracted_via_reasoning` flag.

Schema:
  entity_resolution_cache:
    - cache_key VARCHAR(64) PRIMARY KEY (sha256 hex)
    - payload JSONB NOT NULL
    - created_at TIMESTAMPTZ DEFAULT now()
    - expires_at TIMESTAMPTZ NOT NULL
    - hits_count INTEGER DEFAULT 0

  zoom_recordings ADD COLUMN extracted_via_reasoning BOOLEAN DEFAULT FALSE
  meeting_recordings ADD COLUMN extracted_via_reasoning BOOLEAN DEFAULT FALSE

Revision ID: 0033_entity_resolution_v2
Revises: 0032_calendar_attendees
Create Date: 2026-05-22
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0033_entity_resolution_v2"
down_revision = "0032_calendar_attendees"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "entity_resolution_cache",
        sa.Column("cache_key", sa.String(64), primary_key=True),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "hits_count", sa.Integer, nullable=False, server_default="0",
        ),
    )
    op.create_index(
        "ix_entity_resolution_cache_expires_at",
        "entity_resolution_cache",
        ["expires_at"],
    )

    # extracted_via_reasoning flag — idempotent skip в FR-CR-05-151 retry
    op.add_column(
        "zoom_recordings",
        sa.Column(
            "extracted_via_reasoning",
            sa.Boolean,
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "meeting_recordings",
        sa.Column(
            "extracted_via_reasoning",
            sa.Boolean,
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("meeting_recordings", "extracted_via_reasoning")
    op.drop_column("zoom_recordings", "extracted_via_reasoning")
    op.drop_index("ix_entity_resolution_cache_expires_at",
                  table_name="entity_resolution_cache")
    op.drop_table("entity_resolution_cache")
