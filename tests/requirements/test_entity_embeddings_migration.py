"""FR-CR-05-219 — pgvector migration + entity_embeddings model.

Structural pins (no live DB needed): the migration links onto the
prior head, enables the extension, and the model's invariants
(dim = 3072, kind constants, unique key) match the migration. The
LIVE check (alembic upgrade head against a pgvector Postgres) is run
by the operator on the isolated test DB — see the epic runbook.
"""
from __future__ import annotations

import pathlib

_MIGRATION = (
    pathlib.Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0037_entity_embeddings.py"
)


def _src() -> str:
    return _MIGRATION.read_text(encoding="utf-8")


def test_migration_revision_chain():
    src = _src()
    assert 'revision = "0037_entity_embeddings"' in src
    # Must link onto the prior head so `alembic upgrade head` is linear.
    assert 'down_revision = "0036_gs_sheet_sync"' in src


def test_migration_enables_vector_extension_idempotently():
    src = _src()
    assert "CREATE EXTENSION IF NOT EXISTS vector" in src
    # Table create is guarded so re-running is safe.
    assert "CREATE TABLE IF NOT EXISTS entity_embeddings" in src
    assert "vector(3072)" in src
    # The unique key that makes upserts deterministic.
    assert "UNIQUE (kind, entity_id, model)" in src


def test_migration_downgrade_keeps_extension():
    src = _src()
    # Downgrade drops our table but NOT the extension (dropping an
    # extension is destructive and other objects may depend on it).
    assert "DROP TABLE IF EXISTS entity_embeddings" in src
    assert "DROP EXTENSION" not in src


def test_no_ann_index_over_2000_dims():
    """pgvector ANN (hnsw/ivfflat) caps at 2000 dims; 3072 would fail
    at CREATE INDEX. Pin that we did NOT add an ANN index (exact scan
    is fine at directory scale)."""
    src = _src()
    assert "USING hnsw" not in src
    assert "USING ivfflat" not in src


def test_model_invariants_match_migration():
    # Imported lazily so a sandbox without pgvector still collects the
    # file-structure tests above. In the image pgvector is installed.
    from app.models.entity_embedding import (
        EMBEDDING_DIM,
        KIND_COUNTERPARTY,
        KIND_EMPLOYEE,
        KIND_TEAM_MEMBER,
        EntityEmbedding,
    )

    assert EMBEDDING_DIM == 3072
    assert {KIND_COUNTERPARTY, KIND_TEAM_MEMBER, KIND_EMPLOYEE} == {
        "counterparty",
        "team_member",
        "employee",
    }
    assert EntityEmbedding.__tablename__ == "entity_embeddings"
