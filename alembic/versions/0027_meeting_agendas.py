"""FR-CR-05-165 — Pre-meeting agenda hub.

Stores one row per Google Calendar event the agenda-runner has
already posted, so repeated ticks within the lead-time window
don't fire a duplicate Slack DM.

Schema (`meeting_agendas`):
  calendar_event_id  String(256)  UNIQUE — Calendar event id
  recurring_event_id String(256)  NULLable — if Calendar marks it
                                 as part of a recurring series
  title              String(512)
  title_normalised   String(512)  NULLable — operator's fuzzy join
                                 key (lower + nfkd + collapse ws)
  scheduled_start_at DateTime(tz)  — when the meeting starts
  posted_at          DateTime(tz)  — when we DM'd Slack
  slack_channel      String(64)    — D... DM or C... channel
  slack_ts           String(64)    — message ts (for future edits)
  google_doc_id      String(128)  NULLable — detailed agenda doc
  google_doc_url     String(512)  NULLable
  prior_meeting_zoom_ids String[] NULLable — recordings used for
                                            the «what was discussed»
                                            section (for audit)

Revision ID: 0027_meeting_agendas
Revises: 0026_counterparty_prompt_batches
Create Date: 2026-05-13 14:00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027_meeting_agendas"
down_revision: Union[str, None] = "0026_counterparty_prompt_batches"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "meeting_agendas",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("calendar_event_id", sa.String(256), nullable=False),
        sa.Column("recurring_event_id", sa.String(256), nullable=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("title_normalised", sa.String(512), nullable=True),
        sa.Column(
            "scheduled_start_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "posted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("slack_channel", sa.String(64), nullable=False),
        sa.Column("slack_ts", sa.String(64), nullable=True),
        sa.Column("google_doc_id", sa.String(128), nullable=True),
        sa.Column("google_doc_url", sa.String(512), nullable=True),
        sa.Column(
            "prior_meeting_zoom_ids",
            sa.JSON(),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "calendar_event_id",
            name="uq_meeting_agendas_calendar_event_id",
        ),
    )
    op.create_index(
        "ix_meeting_agendas_scheduled_start_at",
        "meeting_agendas",
        ["scheduled_start_at"],
    )
    op.create_index(
        "ix_meeting_agendas_title_normalised",
        "meeting_agendas",
        ["title_normalised"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_meeting_agendas_title_normalised",
        table_name="meeting_agendas",
    )
    op.drop_index(
        "ix_meeting_agendas_scheduled_start_at",
        table_name="meeting_agendas",
    )
    op.drop_table("meeting_agendas")
