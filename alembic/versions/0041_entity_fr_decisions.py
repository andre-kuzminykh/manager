"""FR-EC-CRITIC — entity_fr_decisions: audit log of the FR resolver (additive).

ADDITIVE ONLY. One new append-only table. Touches nothing existing — safe on
the prod app DB. Holds every resolver decision (shadow or applied) so we can
compare against ground truth, calibrate the threshold, and roll back forms.

SPEC_ENTITY_CRITIC_v0.1 §11.

Revision ID: 0041_entity_fr_decisions
Revises: 0040_sheet_task_links
Create Date: 2026-06-04
"""
from __future__ import annotations

from alembic import op

revision = "0041_entity_fr_decisions"
down_revision = "0040_sheet_task_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS entity_fr_decisions (
          id           BIGSERIAL    PRIMARY KEY,
          source       VARCHAR(16)  NOT NULL,         -- zoom | fireflies
          source_id    TEXT         NOT NULL,         -- zoom_id / fireflies_id
          kind         VARCHAR(16),                   -- company | person | team
          mention      TEXT         NOT NULL,
          canonical    TEXT,                          -- NULL = no confident match
          source_list  TEXT,                          -- CRM sheet(s)
          confidence   DOUBLE PRECISION NOT NULL DEFAULT 0,
          applied      BOOLEAN      NOT NULL DEFAULT FALSE,
          shadow       BOOLEAN      NOT NULL DEFAULT TRUE,
          created_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_entity_fr_decisions_source "
        "ON entity_fr_decisions (source, source_id);"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS entity_fr_decisions CASCADE;")
