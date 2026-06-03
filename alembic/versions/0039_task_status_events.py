"""FR-ST-LOG — task_status_events: unified append-only update log (additive).

ADDITIVE ONLY. Creates one brand-new satellite table of `tasks`. Does NOT
touch tasks / task_status_history / any existing table or type. Safe to run
on the prod app DB; `alembic upgrade head` stays green.

SPEC_STATUS_TRACKER_v0.2 §2 — every status/due/owner/comment change (chat,
meetings, manual, rollback) logs one row here (from->to + source + actor),
single source of truth for «что обновлялось» and rollback.

Revision ID: 0039_task_status_events
Revises: 0038_entity_catalog_staging
Create Date: 2026-06-03
"""
from __future__ import annotations

from alembic import op

revision = "0039_task_status_events"
down_revision = "0038_entity_catalog_staging"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS task_status_events (
          id                  BIGSERIAL    PRIMARY KEY,
          task_id             INTEGER      NOT NULL
                                REFERENCES tasks(id) ON DELETE CASCADE,
          source              VARCHAR(16)  NOT NULL,   -- chat/zoom/fireflies/sheet/rollback
          actor               VARCHAR(64),
          field               VARCHAR(16)  NOT NULL,   -- status/due_date/owner/comment
          from_value          TEXT,
          to_value            TEXT,
          comment             TEXT,
          raw_quote           TEXT,
          confidence          DOUBLE PRECISION,
          meeting_source_id   VARCHAR(128),
          meeting_segment_idx INTEGER,
          meeting_ref         JSONB,
          applied             BOOLEAN      NOT NULL DEFAULT TRUE,
          created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
          CONSTRAINT uq_task_status_events_meeting
            UNIQUE (source, meeting_source_id, meeting_segment_idx, task_id)
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_task_status_events_task_id "
        "ON task_status_events (task_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_task_status_events_created_at "
        "ON task_status_events (created_at);"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS task_status_events CASCADE;")
