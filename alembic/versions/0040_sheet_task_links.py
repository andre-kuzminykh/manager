"""FR-SS-ID — sheet_task_links: row_uuid<->task_id bridge (additive).

ADDITIVE ONLY. One new satellite table of `tasks`. Does NOT touch tasks,
gs_* (System B), or google_sheets_sync (System A). Safe on the prod app DB.

SPEC_SHEET_SYNC_v0.1 §2 — DeveloperMetadata identity for the bidirectional
Sheet bridge, decoupled from System B's GsRecord machinery.

Revision ID: 0040_sheet_task_links
Revises: 0039_task_status_events
Create Date: 2026-06-03
"""
from __future__ import annotations

from alembic import op

revision = "0040_sheet_task_links"
down_revision = "0039_task_status_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS sheet_task_links (
          id                BIGSERIAL    PRIMARY KEY,
          task_id           INTEGER      NOT NULL
                              REFERENCES tasks(id) ON DELETE CASCADE,
          spreadsheet_id    TEXT         NOT NULL,
          sheet_id          BIGINT,
          row_uuid          VARCHAR(64)  NOT NULL,
          row_number        INTEGER,
          last_payload_hash VARCHAR(64),
          last_synced_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
          created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
          CONSTRAINT uq_sheet_task_links_ss_task UNIQUE (spreadsheet_id, task_id),
          CONSTRAINT uq_sheet_task_links_ss_uuid UNIQUE (spreadsheet_id, row_uuid)
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_sheet_task_links_task_id "
        "ON sheet_task_links (task_id);"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS sheet_task_links CASCADE;")
