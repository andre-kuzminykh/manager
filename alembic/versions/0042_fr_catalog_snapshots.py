"""FR-EC-CRITIC-2 — fr_catalog_snapshots: local replica of Viktor's CRM dump.

ADDITIVE ONLY. One new append-only table holding raw `humanoid_fr_search`
dumps. The resolver reads the latest snapshot instead of the live MCP per
meeting; a daily refresh writes a new row. Touches nothing existing — safe on
the prod app DB.

Revision ID: 0042_fr_catalog_snapshots
Revises: 0041_entity_fr_decisions
Create Date: 2026-06-04
"""
from __future__ import annotations

from alembic import op

revision = "0042_fr_catalog_snapshots"
down_revision = "0041_entity_fr_decisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS fr_catalog_snapshots (
          id            BIGSERIAL   PRIMARY KEY,
          raw_text      TEXT        NOT NULL,        -- raw MCP humanoid_fr_search dump
          entity_count  INTEGER     NOT NULL DEFAULT 0,
          fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_fr_catalog_snapshots_fetched_at "
        "ON fr_catalog_snapshots (fetched_at DESC);"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS fr_catalog_snapshots CASCADE;")
