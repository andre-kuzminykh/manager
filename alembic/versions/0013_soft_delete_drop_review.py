"""FR-CR-04-20: Soft-delete + drop the `review` task status.

Two related cleanups bundled into one revision:

1. Adds `tasks.deleted_at` (nullable timestamp). When non-null, the row is
   soft-deleted: hidden from the UI, digests, plans, workload, but kept in
   the DB and visible in audit logs. `Delete task` action sets it.

2. Removes the `review` enum value from `task_status`. Any task that was
   stuck in `review` is moved to `in_progress` (data migration) so the
   enum can be tightened to 4 values (backlog, todo, in_progress, done).

   On Postgres the ENUM type can't be ALTERed to remove a value, so we do
   the standard "rename old, create new, swap" dance.

Revision ID: 0013_soft_delete_drop_review
Revises: 0012_task_recurring
Create Date: 2026-04-27 18:00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013_soft_delete_drop_review"
down_revision: Union[str, None] = "0012_task_recurring"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NEW_VALUES = ("backlog", "todo", "in_progress", "done")


def upgrade() -> None:
    # 1) tasks.deleted_at -------------------------------------------------
    op.add_column(
        "tasks",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_tasks_deleted_at", "tasks", ["deleted_at"], unique=False
    )

    # 2) data: review → in_progress  (covers tasks + history rows) -------
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE tasks SET status = 'in_progress' WHERE status = 'review'"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE task_status_history SET to_status = 'in_progress' "
            "WHERE to_status = 'review'"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE task_status_history SET from_status = 'in_progress' "
            "WHERE from_status = 'review'"
        )
    )

    # 3) drop the review value from the ENUM (Postgres only — SQLite
    #    stores enums as plain strings, so the data migration above is
    #    sufficient). The Postgres dance: rename old type, create new,
    #    cast columns, drop old type.
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE task_status RENAME TO task_status_old")
        new_enum = sa.Enum(*_NEW_VALUES, name="task_status")
        new_enum.create(bind, checkfirst=False)
        op.execute(
            "ALTER TABLE tasks "
            "ALTER COLUMN status TYPE task_status "
            "USING status::text::task_status"
        )
        op.execute(
            "ALTER TABLE task_status_history "
            "ALTER COLUMN from_status TYPE task_status "
            "USING from_status::text::task_status"
        )
        op.execute(
            "ALTER TABLE task_status_history "
            "ALTER COLUMN to_status TYPE task_status "
            "USING to_status::text::task_status"
        )
        op.execute("DROP TYPE task_status_old")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE task_status RENAME TO task_status_new")
        old_enum = sa.Enum(
            "backlog", "todo", "in_progress", "review", "done",
            name="task_status",
        )
        old_enum.create(bind, checkfirst=False)
        op.execute(
            "ALTER TABLE tasks "
            "ALTER COLUMN status TYPE task_status "
            "USING status::text::task_status"
        )
        op.execute(
            "ALTER TABLE task_status_history "
            "ALTER COLUMN from_status TYPE task_status "
            "USING from_status::text::task_status"
        )
        op.execute(
            "ALTER TABLE task_status_history "
            "ALTER COLUMN to_status TYPE task_status "
            "USING to_status::text::task_status"
        )
        op.execute("DROP TYPE task_status_new")

    op.drop_index("ix_tasks_deleted_at", table_name="tasks")
    op.drop_column("tasks", "deleted_at")
