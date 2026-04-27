"""Capture every Slack event verbatim + audio transcripts.

Revision ID: 0008_archive_and_transcript
Revises: 0007_draft_task_link
Create Date: 2026-04-24 19:30:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_archive_and_transcript"
down_revision: Union[str, None] = "0007_draft_task_link"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── slack_messages: extend with subtype, transcript, has_audio ─────
    op.add_column(
        "slack_messages",
        sa.Column("subtype", sa.String(64), nullable=True),
    )
    op.add_column(
        "slack_messages",
        sa.Column("transcript", sa.Text(), nullable=True),
    )
    op.add_column(
        "slack_messages",
        sa.Column(
            "has_audio",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    # ── slack_events_archive: one row per Slack event, no filters ──────
    op.create_table(
        "slack_events_archive",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("event_id", sa.String(64), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("subtype", sa.String(64), nullable=True),
        sa.Column("conversation_id", sa.String(64), nullable=True),
        sa.Column("ts", sa.String(32), nullable=True),
        sa.Column("thread_ts", sa.String(32), nullable=True),
        sa.Column("user_id", sa.String(64), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("transcript", sa.Text(), nullable=True),
        sa.Column(
            "raw", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_slack_events_archive_event_id",
        "slack_events_archive",
        ["event_id"],
    )
    op.create_index(
        "ix_slack_events_archive_conv_ts",
        "slack_events_archive",
        ["conversation_id", "ts"],
    )
    op.create_index(
        "ix_slack_events_archive_received_at",
        "slack_events_archive",
        ["received_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_slack_events_archive_received_at", table_name="slack_events_archive")
    op.drop_index("ix_slack_events_archive_conv_ts", table_name="slack_events_archive")
    op.drop_index("ix_slack_events_archive_event_id", table_name="slack_events_archive")
    op.drop_table("slack_events_archive")
    op.drop_column("slack_messages", "has_audio")
    op.drop_column("slack_messages", "transcript")
    op.drop_column("slack_messages", "subtype")
