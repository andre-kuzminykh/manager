"""FR-CB2-200 — CEO Brain Bot tables.

Adds:
  * ``slack_message_archive`` — append-only archive of every
    Slack message in channels where the CEO Brain Bot is a
    member. UNIQUE(channel_id, ts) so retries are idempotent.
  * ``claude_responder_runs`` — per @mention / DM-reply run
    record with cost, tool_uses, status.

Revision ID: 0031_ceo_brain
Revises: 0030_counterparty_briefs
Create Date: 2026-05-18
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0031_ceo_brain"
down_revision: Union[str, None] = "0030_counterparty_briefs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "slack_message_archive",
        sa.Column(
            "id", sa.Integer(), primary_key=True, autoincrement=True,
        ),
        sa.Column("channel_id", sa.String(64), nullable=False),
        sa.Column("channel_name", sa.String(255), nullable=True),
        sa.Column("ts", sa.String(64), nullable=False),
        sa.Column("thread_ts", sa.String(64), nullable=True),
        sa.Column("user_id", sa.String(64), nullable=True),
        sa.Column("user_display_name", sa.String(255), nullable=True),
        sa.Column("subtype", sa.String(64), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("raw_payload", sa.JSON(), nullable=True),
        sa.Column(
            "edit_count", sa.Integer(), nullable=False, server_default="0",
        ),
        sa.Column(
            "deleted_at", sa.DateTime(timezone=True), nullable=True,
        ),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "channel_id", "ts",
            name="uq_slack_message_archive_channel_ts",
        ),
    )
    op.create_index(
        "ix_slack_message_archive_day", "slack_message_archive", ["day"],
    )
    op.create_index(
        "ix_slack_message_archive_channel_day",
        "slack_message_archive", ["channel_id", "day"],
    )

    op.create_table(
        "claude_responder_runs",
        sa.Column(
            "id", sa.Integer(), primary_key=True, autoincrement=True,
        ),
        sa.Column("slack_channel_id", sa.String(64), nullable=False),
        sa.Column("slack_event_ts", sa.String(64), nullable=False),
        sa.Column("slack_placeholder_ts", sa.String(64), nullable=True),
        sa.Column("request_payload", sa.JSON(), nullable=True),
        sa.Column("response_text", sa.Text(), nullable=True),
        sa.Column("tool_uses", sa.JSON(), nullable=True),
        sa.Column(
            "status", sa.String(32), nullable=False, server_default="pending",
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "cost_usd", sa.Numeric(10, 4), nullable=False, server_default="0",
        ),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_claude_responder_runs_channel_ts",
        "claude_responder_runs",
        ["slack_channel_id", "slack_event_ts"],
    )
    op.create_index(
        "ix_claude_responder_runs_status",
        "claude_responder_runs",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_claude_responder_runs_status", "claude_responder_runs",
    )
    op.drop_index(
        "ix_claude_responder_runs_channel_ts", "claude_responder_runs",
    )
    op.drop_table("claude_responder_runs")
    op.drop_index(
        "ix_slack_message_archive_channel_day", "slack_message_archive",
    )
    op.drop_index(
        "ix_slack_message_archive_day", "slack_message_archive",
    )
    op.drop_table("slack_message_archive")
