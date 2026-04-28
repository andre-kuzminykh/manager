"""FR-CR-04-27 — Telegram live listener state.

Single-row table that stores the last `update_id` we acknowledged
to Telegram's getUpdates long-polling API. On restart the listener
resumes from this offset so we don't miss / double-process updates
that arrived while the worker was down.

(Telegram's server-side queue retains undelivered updates for ~24h,
so a longer outage means some updates are lost — that's an
acceptable trade-off for a free-tier setup. Catastrophic loss is
backstopped by the Supabase view path, which has the full archive.)

Revision ID: 0015_telegram_listener_state
Revises: 0014_telegram_source
Create Date: 2026-04-28 17:00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015_telegram_listener_state"
down_revision: Union[str, None] = "0014_telegram_source"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "telegram_listener_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("last_update_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("id = 1", name="telegram_listener_state_singleton"),
    )


def downgrade() -> None:
    op.drop_table("telegram_listener_state")
