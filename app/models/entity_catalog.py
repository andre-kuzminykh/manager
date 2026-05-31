"""FR-CR-05-231 — deduplicated entity catalog (staging build).

Operator-pinned 2026-05-31: «составь список из вот всего что я скинул,
мне надо новую бд сделать и с ллм собрать контекст рядом — мне нужно
название, флаг это организация или физлицо, если физлицо то к какой
организации относится и в description полное описание, для векторного
поиска берём название и описание. Это будет новая таблица без дублей,
давай сначала сделаем временную, а потом заменим вместо той».

Two satellite tables, both ADDITIVE (a bug here cannot corrupt the live
`counterparties` / `team_members` / `employees` directory):

* ``EntityCatalogStaging`` — the deduplicated catalog itself. One row per
  distinct entity, keyed by ``(name_normalised, is_org)``. ``is_org``
  flags organisation vs natural person; ``parent_org`` carries a person's
  affiliation. ``description`` is the LLM-assembled context; vector search
  later embeds ``name + description`` (mirrors the FR-CR-05-219 satellite).

* ``EntityCatalogIngest`` — per-chunk checkpoint so the builder
  (``ops/build_entity_catalog``) is resumable and idempotent: a chunk that
  is already recorded ``done`` is skipped, so the operator can ingest the
  1.4 MB source «понемного» (a few chunks per run) without the context /
  cost blowing up, and re-runs make ZERO new LLM calls.

Staging-first by design: build + eyeball here, then swap over the live
directory in a separate, reversible step (FR-CR-05-231 follow-up).
"""
from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# Checkpoint statuses for the chunk ledger.
INGEST_DONE = "done"
INGEST_ERROR = "error"


class EntityCatalogStaging(Base, TimestampMixin):
    __tablename__ = "entity_catalog_staging"
    __table_args__ = (
        UniqueConstraint(
            "name_normalised", "is_org", name="uq_entity_catalog_norm_isorg"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Canonical display name (kept as the most complete surface form seen).
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # Fuzzy-match key (app.sync.counterparties.normalise_name); dedup anchor.
    name_normalised: Mapped[str] = mapped_column(
        String(512), nullable=False, index=True
    )
    # True = organisation (company / fund / gov body), False = natural person.
    is_org: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # For persons: the organisation they belong to (NULL for orgs / unknown).
    parent_org: Mapped[str | None] = mapped_column(Text, nullable=True)
    # LLM-assembled context. Embedded together with `name` for vector search.
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Newline-joined distinct surface variants seen across chunks (provenance).
    aliases: Mapped[str | None] = mapped_column(Text, nullable=True)
    # How many source chunks contributed to this row (merge counter).
    mentions_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )


class EntityCatalogIngest(Base, TimestampMixin):
    __tablename__ = "entity_catalog_ingest"

    # Deterministic 0-based chunk index from `iter_source_chunks`; PK so a
    # re-run skips chunks already marked done (resume / idempotency).
    chunk_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    row_lo: Mapped[int] = mapped_column(Integer, nullable=False)
    row_hi: Mapped[int] = mapped_column(Integer, nullable=False)
    char_len: Mapped[int] = mapped_column(Integer, nullable=False)
    entities_found: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=INGEST_DONE
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = [
    "EntityCatalogStaging",
    "EntityCatalogIngest",
    "INGEST_DONE",
    "INGEST_ERROR",
]
