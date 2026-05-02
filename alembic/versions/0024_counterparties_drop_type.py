"""FR-CR-05-132 — drop `counterparties.type` column.

Operator-pinned: one canonical row per real counterparty,
identified solely by `name_normalised`. Source-tab labels
(formerly stored on the hub as `type` — «Outreach»,
«Rejections», «Financial/VC», etc.) live on the satellite
(`counterparty_attrs.attributes` JSON) where they belong.

This migration:
  - Collapses any `(name_normalised, type)` duplicate hubs
    into a single hub (lowest-id wins). Re-points
    `counterparty_attrs.counterparty_id` and
    `counterparty_mentions.counterparty_id` to the surviving
    hub before deleting the duplicates so the FKs hold.
  - Drops the `(name_normalised, type)` UNIQUE constraint
    (`uq_counterparties_name_norm_type`) and replaces it with
    `(name_normalised)` UNIQUE
    (`uq_counterparties_name_normalised`).
  - Drops the `type` column.

Revision ID: 0024_counterparties_drop_type
Revises: 0023_counterparty_mentions
Create Date: 2026-05-02 12:00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024_counterparties_drop_type"
down_revision: Union[str, None] = "0023_counterparty_mentions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    # 1) Collapse duplicate hubs: for each `name_normalised`
    # group, keep the lowest id. Repoint dependent rows to the
    # winner, then delete the losers.
    bind.execute(sa.text("""
        WITH dup AS (
            SELECT id,
                   MIN(id) OVER (PARTITION BY name_normalised) AS keep_id
              FROM counterparties
        )
        UPDATE counterparty_attrs
           SET counterparty_id = dup.keep_id
          FROM dup
         WHERE counterparty_attrs.counterparty_id = dup.id
           AND dup.id <> dup.keep_id
    """))
    bind.execute(sa.text("""
        WITH dup AS (
            SELECT id,
                   MIN(id) OVER (PARTITION BY name_normalised) AS keep_id
              FROM counterparties
        )
        UPDATE counterparty_mentions
           SET counterparty_id = dup.keep_id
          FROM dup
         WHERE counterparty_mentions.counterparty_id = dup.id
           AND dup.id <> dup.keep_id
    """))
    # `counterparty_attrs` UNIQUE(counterparty_id, source) might
    # collide after the repoint above (same source label on two
    # losers both repointed to the winner). Drop the now-dupe
    # satellite rows; we keep the lowest-id satellite per
    # (counterparty_id, source).
    bind.execute(sa.text("""
        DELETE FROM counterparty_attrs a
         USING counterparty_attrs b
         WHERE a.counterparty_id = b.counterparty_id
           AND a.source          = b.source
           AND a.id              > b.id
    """))
    # Same for mentions: UNIQUE(source_kind, source_id,
    # counterparty_id) might collide.
    bind.execute(sa.text("""
        DELETE FROM counterparty_mentions a
         USING counterparty_mentions b
         WHERE a.source_kind     = b.source_kind
           AND a.source_id       = b.source_id
           AND a.counterparty_id = b.counterparty_id
           AND a.id              > b.id
    """))
    bind.execute(sa.text("""
        DELETE FROM counterparties
         WHERE id NOT IN (
             SELECT MIN(id) FROM counterparties
              GROUP BY name_normalised
         )
    """))

    # 2) Swap UNIQUE constraint.
    op.drop_constraint(
        "uq_counterparties_name_norm_type",
        "counterparties",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_counterparties_name_normalised",
        "counterparties",
        ["name_normalised"],
    )

    # 3) Drop the column.
    op.drop_column("counterparties", "type")


def downgrade() -> None:
    op.add_column(
        "counterparties",
        sa.Column(
            "type",
            sa.String(64),
            nullable=False,
            server_default="uncategorised",
        ),
    )
    op.drop_constraint(
        "uq_counterparties_name_normalised",
        "counterparties",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_counterparties_name_norm_type",
        "counterparties",
        ["name_normalised", "type"],
    )
