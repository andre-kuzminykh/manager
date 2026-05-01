"""FR-CR-05-125 — counterparty mentions junction table.

For every meeting (Fireflies / Zoom) the LLM matcher finds in
the transcript, we store a row linking the recording to the
counterparty hub. Lets the operator query «all meetings
mentioning ADNOC», audit which mentions stuck after canonical
fuzzy matching, and re-derive the doc/summary annotations
without re-calling the LLM.

`source_kind` discriminator + `source_id` (the platform's
native recording UUID — fireflies_id / zoom_id) keeps the
table single-shape across both meeting sources without two
nullable FKs.

UNIQUE(source_kind, source_id, counterparty_id) — one mention
row per (recording, counterparty) pair. Re-runs of the same
recording (operator pinned via `--rerun`) replace earlier
mentions cleanly.

Revision ID: 0023_counterparty_mentions
Revises: 0022_counterparties
Create Date: 2026-05-01 18:30:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023_counterparty_mentions"
down_revision: Union[str, None] = "0022_counterparties"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "counterparty_mentions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("counterparty_id", sa.Integer(), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(128), nullable=False),
        sa.Column("context", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["counterparty_id"], ["counterparties.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "source_kind", "source_id", "counterparty_id",
            name="uq_counterparty_mentions_source_cp",
        ),
    )
    op.create_index(
        "ix_counterparty_mentions_source",
        "counterparty_mentions",
        ["source_kind", "source_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_counterparty_mentions_source",
        table_name="counterparty_mentions",
    )
    op.drop_table("counterparty_mentions")
