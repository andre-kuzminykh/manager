"""FR-CR-05-118 — add 'zoom' to task_source_kind enum.

The 0020 migration created the `zoom_recordings` table but
forgot to extend the `task_source_kind` enum, so any task
created from a Zoom recording crashes with
«invalid input value for enum task_source_kind: "zoom"».

Mirrors how 0018 added 'fireflies' to the same enum.

Revision ID: 0021_task_source_kind_zoom
Revises: 0020_zoom_recordings
Create Date: 2026-04-30 20:30:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0021_task_source_kind_zoom"
down_revision: Union[str, None] = "0020_zoom_recordings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Postgres-only path; SQLite (test fixture) uses a string
    # column under the hood so the ALTER TYPE is a no-op.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                "ALTER TYPE task_source_kind ADD VALUE IF NOT EXISTS 'zoom'"
            )


def downgrade() -> None:
    # Enum value removal in Postgres requires recreating the
    # type; not worth it for a forward-only schema. No-op.
    pass
