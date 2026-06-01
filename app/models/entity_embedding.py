"""FR-CR-05-219 — embeddings satellite for directory entities.

`EntityEmbedding` stores ONE current embedding per (kind, entity_id,
model) so the Zoom/Fireflies entity-matcher (FR-CR-05-222) can run a
point pgvector cosine search over a single `kind` instead of stuffing
the whole directory into the LLM prompt.

Satellite design — never FK-references the source rows. `entity_id`
is the source row id as TEXT (counterparties.id, team_members.id, or
employees.slack_user_id). `text_repr` is exactly what was embedded;
`text_repr_hash` (sha256) lets the refresh cron (FR-CR-05-221) detect
when a source row changed and needs re-embedding.

3072 dims = text-embedding-3-large. No ANN index (pgvector ANN caps at
2000 dims); exact cosine scan over ~12.6k rows is sub-50ms.
"""
from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# Embedding dimensionality for text-embedding-3-large. Kept here as the
# single source of truth so the model, service and migration agree.
EMBEDDING_DIM = 3072

# Entity kinds (mirrored in the service layer).
KIND_COUNTERPARTY = "counterparty"
KIND_TEAM_MEMBER = "team_member"
KIND_EMPLOYEE = "employee"
KIND_TASK = "task"  # FR-TV — semantic task search / NL status updates


class EntityEmbedding(Base, TimestampMixin):
    __tablename__ = "entity_embeddings"
    __table_args__ = (
        UniqueConstraint("kind", "entity_id", "model", name="uq_entity_emb_kind_id_model"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    text_repr: Mapped[str] = mapped_column(Text, nullable=False)
    text_repr_hash: Mapped[str] = mapped_column(String(64), nullable=False)


__all__ = [
    "EntityEmbedding",
    "EMBEDDING_DIM",
    "KIND_COUNTERPARTY",
    "KIND_TEAM_MEMBER",
    "KIND_EMPLOYEE",
]
