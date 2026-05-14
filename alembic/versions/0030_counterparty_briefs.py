"""FR-CR-05-168 — Counterparty Briefs (event-trigger + per-counterparty Doc cache).

Two tables:
  * `counterparty_briefs_events` — per-event idempotency. One row
    per Calendar event we've processed. `calendar_event_id` UNIQUE.
  * `counterparty_briefs`        — per-counterparty Doc cache.
    One row per (kind, counterparty_key) we've ever briefed.
    `counterparty_key` UNIQUE. TTL-refreshable.

Plus the link table `counterparty_brief_links` (event ↔ brief).

Revision ID: 0030_counterparty_briefs
Revises: 0028_zoom_recordings_host_email
Create Date: 2026-05-14 14:00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0030_counterparty_briefs"
down_revision: Union[str, None] = "0028_zoom_recordings_host_email"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "counterparty_briefs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("counterparty_key", sa.String(256), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),  # 'org' | 'person'
        sa.Column("display_name", sa.String(512), nullable=False),
        sa.Column("org_name", sa.String(512), nullable=True),
        sa.Column("counterparty_id", sa.Integer(), nullable=True),
        sa.Column("research_payload", sa.JSON(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.Column("google_doc_id", sa.String(128), nullable=True),
        sa.Column("google_doc_url", sa.String(512), nullable=True),
        sa.Column(
            "researched_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "counterparty_key",
            name="uq_counterparty_briefs_counterparty_key",
        ),
    )
    op.create_index(
        "ix_counterparty_briefs_kind",
        "counterparty_briefs", ["kind"],
    )
    op.create_index(
        "ix_counterparty_briefs_researched_at",
        "counterparty_briefs", ["researched_at"],
    )

    op.create_table(
        "counterparty_briefs_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("calendar_event_id", sa.String(256), nullable=False),
        sa.Column("event_title", sa.String(512), nullable=False),
        sa.Column(
            "scheduled_meeting_at",
            sa.DateTime(timezone=True), nullable=False,
        ),
        sa.Column(
            "posted_at",
            sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("slack_channel", sa.String(64), nullable=False),
        sa.Column("slack_ts", sa.String(64), nullable=True),
        sa.Column("total_cost_usd", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.Column("link_summary", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "calendar_event_id",
            name="uq_counterparty_briefs_events_calendar_event_id",
        ),
    )
    op.create_index(
        "ix_counterparty_briefs_events_scheduled",
        "counterparty_briefs_events", ["scheduled_meeting_at"],
    )

    op.create_table(
        "counterparty_brief_links",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("brief_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"], ["counterparty_briefs_events.id"],
            ondelete="CASCADE",
            name="fk_cb_links_event",
        ),
        sa.ForeignKeyConstraint(
            ["brief_id"], ["counterparty_briefs.id"],
            ondelete="CASCADE",
            name="fk_cb_links_brief",
        ),
        sa.UniqueConstraint(
            "event_id", "brief_id",
            name="uq_counterparty_brief_links_event_brief",
        ),
    )


def downgrade() -> None:
    op.drop_table("counterparty_brief_links")
    op.drop_index(
        "ix_counterparty_briefs_events_scheduled",
        table_name="counterparty_briefs_events",
    )
    op.drop_table("counterparty_briefs_events")
    op.drop_index(
        "ix_counterparty_briefs_researched_at",
        table_name="counterparty_briefs",
    )
    op.drop_index(
        "ix_counterparty_briefs_kind",
        table_name="counterparty_briefs",
    )
    op.drop_table("counterparty_briefs")
