"""FR-CR-05-219 — pgvector + entity_embeddings table (isolated, additive).

ADDITIVE ONLY. Enables the `vector` extension and creates ONE new table,
`entity_embeddings`, that holds embeddings for directory entities
(counterparties / team_members / employees) so the Zoom/Fireflies
entity-matcher can do point pgvector retrieval instead of stuffing the
whole directory into the prompt.

Does NOT touch any existing table — the embeddings live in a satellite
table keyed by (kind, entity_id, model), so a bug here cannot corrupt
counterparties / team_members / employees.

Dimensionality note: text-embedding-3-large = 3072 dims. pgvector ANN
indexes (HNSW / IVFFlat) cap at 2000 dims, so we DO NOT build an ANN
index here — at directory scale (~12.6k rows) an exact cosine scan is
sub-50ms. A btree on `kind` keeps the per-kind filter cheap. HNSW via
`halfvec` is a future optimisation (FR-CR-05-219 follow-up) and can be
added in a later migration without touching this one.

Revision ID: 0037_entity_embeddings
Revises: 0036_gs_sheet_sync
Create Date: 2026-05-31
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0037_entity_embeddings"
down_revision = "0036_gs_sheet_sync"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # FR-CR-05-241 — SEPARATE-INSTANCE topology (operator decision
    # 2026-06-01): the vector catalog lives in a DEDICATED pgvector DB
    # (CATALOG_DATABASE_URL), so the prod app DB (postgres:*-alpine, no
    # pgvector) must NEVER get this table. If the `vector` extension isn't
    # available, SKIP gracefully so `alembic upgrade head` stays green on the
    # prod DB without touching it. On the pgvector catalog DB this runs in
    # full (idempotent — IF NOT EXISTS).
    conn = op.get_bind()
    has_pgvector = conn.execute(
        text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
    ).scalar() is not None
    if not has_pgvector:
        print(
            "0037: pgvector unavailable — skipping entity_embeddings "
            "(catalog lives in the separate pgvector instance, prod DB "
            "untouched)"
        )
        return

    # pgvector extension. Requires the pgvector/pgvector image (NOT
    # postgres:*-alpine, which ships without it). Safe to re-run.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS entity_embeddings (
          id              BIGSERIAL PRIMARY KEY,
          kind            VARCHAR(32)  NOT NULL,   -- 'counterparty' | 'team_member' | 'employee'
          entity_id       VARCHAR(64)  NOT NULL,   -- source row id (as text)
          model           VARCHAR(64)  NOT NULL,   -- e.g. 'text-embedding-3-large'
          dim             INTEGER      NOT NULL,   -- 3072
          embedding       vector(3072) NOT NULL,
          text_repr       TEXT         NOT NULL,   -- exact text that was embedded
          text_repr_hash  VARCHAR(64)  NOT NULL,   -- sha256(text_repr) for staleness check
          created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
          updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
          -- one current embedding per (entity, model)
          UNIQUE (kind, entity_id, model)
        );
        """
    )
    # Cheap per-kind filter (we always scope the cosine scan to one kind).
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_entity_embeddings_kind "
        "ON entity_embeddings (kind);"
    )
    # Fast staleness lookup: find rows whose text_repr_hash changed.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_entity_embeddings_kind_hash "
        "ON entity_embeddings (kind, text_repr_hash);"
    )


def downgrade() -> None:
    # Drop ONLY our table. We deliberately DO NOT drop the `vector`
    # extension — other objects might come to depend on it, and dropping
    # an extension is destructive. Leaving it is harmless.
    op.execute("DROP TABLE IF EXISTS entity_embeddings CASCADE;")
