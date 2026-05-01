"""FR-CR-05-124 — counterparties hub + satellite (data-vault style).

Hub `counterparties`: identity (name, type) — one row per real
counterparty (company / fund / investor / partner / etc.).
Satellite `counterparty_attrs`: per-source descriptive payload
in JSONB. The same hub can have multiple satellites (one per
sheet / tab the row was seen in).

Use cases:
- Speech-recognition mentions in meeting transcripts get fuzzy-
  matched against the canonical `name` on the hub.
- Rich source data (status, role, comments, links) lives on the
  satellite — keeps the hub minimal so name lookups are fast.

Revision ID: 0022_counterparties
Revises: 0021_task_source_kind_zoom
Create Date: 2026-05-01 17:00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022_counterparties"
down_revision: Union[str, None] = "0021_task_source_kind_zoom"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "counterparties",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        # Lowercase / ASCII-folded form for fast fuzzy lookup.
        sa.Column("name_normalised", sa.String(512), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "name_normalised", "type",
            name="uq_counterparties_name_norm_type",
        ),
    )
    op.create_index(
        "ix_counterparties_name_normalised",
        "counterparties", ["name_normalised"],
    )

    op.create_table(
        "counterparty_attrs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "counterparty_id", sa.Integer(), nullable=False,
        ),
        # Free-form source label — e.g. "Status outreach",
        # "Outreach", "Rejections", "Looking for intros".
        sa.Column("source", sa.String(128), nullable=False),
        # All sheet columns as JSON; portable across SQLite tests
        # (TEXT) and Postgres prod (JSONB).
        sa.Column("attributes", sa.JSON(), nullable=False),
        sa.Column(
            "captured_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["counterparty_id"], ["counterparties.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "counterparty_id", "source",
            name="uq_counterparty_attrs_id_source",
        ),
    )


def downgrade() -> None:
    op.drop_table("counterparty_attrs")
    op.drop_index(
        "ix_counterparties_name_normalised", table_name="counterparties",
    )
    op.drop_table("counterparties")
