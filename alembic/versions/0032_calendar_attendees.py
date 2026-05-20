"""FR-CR-05-169 — add `calendar_attendees` JSON column to recording tables.

Stores the resolved attendee list from the matching Google Calendar
event for each Zoom / Fireflies recording. Shape per row:

    [
      {
        "email": "artem@thehumanoid.ai",
        "display_name": "Artem Sokolov",
        "resolved_name": "Артем Соколов",
        "source": "team_member" | "employee" | "counterparty" | "unknown",
        "response_status": "accepted" | "tentative" | "declined" | "needsAction"
      },
      ...
    ]

Nullable on purpose — old rows backfilled to NULL; downstream
summary builder falls back to the LLM-based `participants` field
when this column is empty.

Revision ID: 0032_calendar_attendees
Revises: 0031_ceo_brain
Create Date: 2026-05-20
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0032_calendar_attendees"
down_revision: Union[str, None] = "0031_ceo_brain"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "zoom_recordings",
        sa.Column("calendar_attendees", sa.JSON, nullable=True),
    )
    op.add_column(
        "meeting_recordings",
        sa.Column("calendar_attendees", sa.JSON, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("meeting_recordings", "calendar_attendees")
    op.drop_column("zoom_recordings", "calendar_attendees")



