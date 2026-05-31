"""FR-CR-05-231 — entity_catalog_staging + ingest ledger (additive).

ADDITIVE ONLY. Creates the staging catalog the operator asked for
(«новая таблица без дублей … сначала временную, потом заменим вместо
той») plus a per-chunk ingest ledger so the LLM builder is resumable.

Does NOT touch counterparties / team_members / employees / the live
directory in any way — both tables are brand-new satellites. The
eventual swap onto the live directory is a separate, reversible step.

Revision ID: 0038_entity_catalog_staging
Revises: 0037_entity_embeddings
Create Date: 2026-05-31
"""
from __future__ import annotations

from alembic import op

revision = "0038_entity_catalog_staging"
down_revision = "0037_entity_embeddings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS entity_catalog_staging (
          id              BIGSERIAL PRIMARY KEY,
          name            TEXT         NOT NULL,   -- canonical display form
          name_normalised VARCHAR(512) NOT NULL,   -- fuzzy-match dedup key
          is_org          BOOLEAN      NOT NULL,   -- true=org, false=person
          parent_org      TEXT,                    -- person's affiliation
          description     TEXT         NOT NULL DEFAULT '',
          aliases         TEXT,                    -- newline-joined variants
          mentions_count  INTEGER      NOT NULL DEFAULT 1,
          created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
          updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
          -- one row per distinct entity; org vs person kept separate so a
          -- company and an eponymous founder don't collide.
          UNIQUE (name_normalised, is_org)
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_entity_catalog_staging_norm "
        "ON entity_catalog_staging (name_normalised);"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS entity_catalog_ingest (
          chunk_no        INTEGER      PRIMARY KEY,  -- deterministic chunk idx
          row_lo          INTEGER      NOT NULL,
          row_hi          INTEGER      NOT NULL,
          char_len        INTEGER      NOT NULL,
          entities_found  INTEGER      NOT NULL DEFAULT 0,
          status          VARCHAR(16)  NOT NULL DEFAULT 'done',
          note            TEXT,
          created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
          updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS entity_catalog_ingest CASCADE;")
    op.execute("DROP TABLE IF EXISTS entity_catalog_staging CASCADE;")
