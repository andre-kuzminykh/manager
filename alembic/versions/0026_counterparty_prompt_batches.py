"""FR-CR-05-138 — batch multi-select enrollment widget for
unresolved counterparty mentions (UX rev2 of FR-CR-05-133).

Replaces the previous one-widget-per-entity flow with a single
list-with-numpad message:

  Stage 1 — multi-select grid (`counterparty_prompt_batches`):
    «Found N unrecognised entities — tap numbers to select»
    [1][2][3][4][5]
    [6][7][8][9][10]
    [Next →]

  Stage 2 — per selected entity, in sequence:
    «Processing entity 2 of 5: «<surface form>».
     Reply with the correct name (text or voice), or:
     [✅ Keep] [⏭ Skip]»

  Stage 3 — context for each kept entity:
    «<canonical name>: send context (text or voice) or [⏭ Skip]»

  Final — terminal recap:
    «Done. Added N entities (M with context, K without).»

Schema:
  counterparty_prompt_batches — one row per (recording, user)
    multiselect_message_id BigInt — the list-with-numpad msg
    entity_count, status (pending_selection|processing|completed),
    current_index Int — pointer into the selected list.

  counterparty_prompts (extended from FR-CR-05-133):
    + batch_id FK
    + index_in_batch Int (1-based, displayed in the grid)
    + selected Bool — flipped when user toggles the index
    + canonical_name_corrected Text — operator's text/voice reply
      from stage 2; falls back to mention_text if they pressed
      «Keep».

Revision ID: 0026_counterparty_prompt_batches
Revises: 0025_counterparty_prompts
Create Date: 2026-05-04 06:30:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0026_counterparty_prompt_batches"
down_revision: Union[str, None] = "0025_counterparty_prompts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "counterparty_prompt_batches",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(128), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("multiselect_message_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "entity_count", sa.Integer(), nullable=False,
            server_default="0",
        ),
        sa.Column(
            "status", sa.String(32), nullable=False,
            server_default="pending_selection",
        ),
        sa.Column("current_index", sa.Integer(), nullable=True),
        sa.Column(
            "current_step", sa.String(32), nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "completed_at", sa.DateTime(timezone=True), nullable=True,
        ),
        sa.UniqueConstraint(
            "source_kind", "source_id", "user_id",
            name="uq_counterparty_prompt_batches_per_user",
        ),
    )
    op.create_index(
        "ix_counterparty_prompt_batches_status",
        "counterparty_prompt_batches", ["status"],
    )

    op.add_column(
        "counterparty_prompts",
        sa.Column("batch_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "counterparty_prompts",
        sa.Column("index_in_batch", sa.Integer(), nullable=True),
    )
    op.add_column(
        "counterparty_prompts",
        sa.Column(
            "selected", sa.Boolean(), nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "counterparty_prompts",
        sa.Column("canonical_name_corrected", sa.String(512), nullable=True),
    )
    op.create_foreign_key(
        "fk_counterparty_prompts_batch_id",
        "counterparty_prompts", "counterparty_prompt_batches",
        ["batch_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index(
        "ix_counterparty_prompts_batch_id",
        "counterparty_prompts", ["batch_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_counterparty_prompts_batch_id",
        table_name="counterparty_prompts",
    )
    op.drop_constraint(
        "fk_counterparty_prompts_batch_id",
        "counterparty_prompts", type_="foreignkey",
    )
    op.drop_column("counterparty_prompts", "canonical_name_corrected")
    op.drop_column("counterparty_prompts", "selected")
    op.drop_column("counterparty_prompts", "index_in_batch")
    op.drop_column("counterparty_prompts", "batch_id")
    op.drop_index(
        "ix_counterparty_prompt_batches_status",
        table_name="counterparty_prompt_batches",
    )
    op.drop_table("counterparty_prompt_batches")
