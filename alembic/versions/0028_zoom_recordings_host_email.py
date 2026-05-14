"""FR-CR-05-166 follow-up — add `host_email` to zoom_recordings.

The Zoom API already returns `host_email` per recording (see
`ZoomRecordingMeta.host_email`); we just never persisted it.
Without this column the pattern-based agenda detector can't
filter «my» recurring meetings from teammates' (e.g. Ирины
«Подземелья» daily group).

Nullable on purpose — old rows backfilled to NULL; downstream
filter treats NULL as «unknown host» and (configurably) skips
them.

Revision ID: 0028_zoom_recordings_host_email
Revises: 0027_meeting_agendas
Create Date: 2026-05-14 00:30:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028_zoom_recordings_host_email"
down_revision: Union[str, None] = "0027_meeting_agendas"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "zoom_recordings",
        sa.Column("host_email", sa.String(256), nullable=True),
    )
    op.create_index(
        "ix_zoom_recordings_host_email",
        "zoom_recordings",
        ["host_email"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_zoom_recordings_host_email",
        table_name="zoom_recordings",
    )
    op.drop_column("zoom_recordings", "host_email")
