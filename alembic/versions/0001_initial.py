"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-23 00:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "slack_conversations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("team_id", sa.String(64), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "slack_messages",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.String(64), sa.ForeignKey("slack_conversations.id"), nullable=False),
        sa.Column("ts", sa.String(32), nullable=False),
        sa.Column("thread_ts", sa.String(32), nullable=True),
        sa.Column("user_id", sa.String(64), nullable=True),
        sa.Column("text", sa.Text, nullable=False, server_default=""),
        sa.Column("permalink", sa.String(512), nullable=True),
        sa.Column("raw", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("conversation_id", "ts", name="uq_slack_messages_conv_ts"),
    )
    op.create_index("ix_slack_messages_thread", "slack_messages", ["thread_ts"])

    op.create_table(
        "context_snapshots",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.String(64), nullable=False),
        sa.Column("source_ts", sa.String(32), nullable=False),
        sa.Column("thread_ts", sa.String(32), nullable=True),
        sa.Column("source_message", sa.JSON, nullable=False),
        sa.Column("history_before", sa.JSON, nullable=False),
        sa.Column("thread_messages", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "processed_slack_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    intent_type = sa.Enum(
        "create_task", "create_meeting", "update_task", "update_meeting", "no_action",
        name="intent_type",
    )
    intent_type.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "intent_inferences",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("context_snapshot_id", sa.Integer, sa.ForeignKey("context_snapshots.id"), nullable=True),
        sa.Column("intent", intent_type, nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("invocation_type", sa.String(32), nullable=False),
        sa.Column("raw", sa.JSON, nullable=True),
        sa.Column("reasoning", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    draft_state = sa.Enum(
        "proposed", "confirmed", "edited", "ignored", "expired", "failed",
        name="action_draft_state",
    )
    draft_state.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "action_drafts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("inference_id", sa.Integer, sa.ForeignKey("intent_inferences.id"), nullable=False),
        sa.Column("intent", intent_type, nullable=False),
        sa.Column("state", draft_state, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("created_by_slack_user_id", sa.String(64), nullable=True),
        sa.Column("slack_message_ts", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    task_priority = sa.Enum("low", "medium", "high", "urgent", name="task_priority")
    task_priority.create(op.get_bind(), checkfirst=True)
    task_status = sa.Enum(
        "backlog", "todo", "in_progress", "review", "done", name="task_status"
    )
    task_status.create(op.get_bind(), checkfirst=True)
    meeting_status = sa.Enum("scheduled", "cancelled", "done", name="meeting_status")
    meeting_status.create(op.get_bind(), checkfirst=True)
    sync_status = sa.Enum("pending", "success", "failed", name="sync_status")
    sync_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("owner_user_id", sa.String(64), nullable=True),
        sa.Column("owner_display_name", sa.String(255), nullable=True),
        sa.Column("priority", task_priority, nullable=False),
        sa.Column("due_date", sa.Date, nullable=True),
        sa.Column("status", task_status, nullable=False),
        sa.Column("source_conversation_id", sa.String(64), nullable=True),
        sa.Column("source_message_ts", sa.String(32), nullable=True),
        sa.Column("source_thread_ts", sa.String(32), nullable=True),
        sa.Column("source_permalink", sa.String(512), nullable=True),
        sa.Column("context_snapshot_id", sa.Integer, sa.ForeignKey("context_snapshots.id"), nullable=True),
        sa.Column("created_by_slack_user_id", sa.String(64), nullable=True),
        sa.Column("google_sheets_row_id", sa.Integer, nullable=True),
        sa.Column("google_tasks_id", sa.String(128), nullable=True),
        sa.Column("extra", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "meetings",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("participants", sa.JSON, nullable=False),
        sa.Column("datetime_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=True),
        sa.Column("status", meeting_status, nullable=False),
        sa.Column("source_conversation_id", sa.String(64), nullable=True),
        sa.Column("source_message_ts", sa.String(32), nullable=True),
        sa.Column("source_thread_ts", sa.String(32), nullable=True),
        sa.Column("source_permalink", sa.String(512), nullable=True),
        sa.Column("context_snapshot_id", sa.Integer, sa.ForeignKey("context_snapshots.id"), nullable=True),
        sa.Column("created_by_slack_user_id", sa.String(64), nullable=True),
        sa.Column("extra", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "google_sheets_sync",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.Integer, sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("spreadsheet_id", sa.String(128), nullable=False),
        sa.Column("row_id", sa.Integer, nullable=True),
        sa.Column("status", sync_status, nullable=False),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "google_tasks_sync",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.Integer, sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("google_user_id", sa.String(128), nullable=True),
        sa.Column("tasklist_id", sa.String(128), nullable=False),
        sa.Column("google_task_id", sa.String(128), nullable=True),
        sa.Column("status", sync_status, nullable=False),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=True),
        sa.Column("entity_id", sa.String(64), nullable=True),
        sa.Column("actor", sa.String(128), nullable=True),
        sa.Column("payload", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "oauth_credentials",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("user_key", sa.String(128), nullable=False),
        sa.Column("access_token_ciphertext", sa.Text, nullable=False),
        sa.Column("refresh_token_ciphertext", sa.Text, nullable=True),
        sa.Column("scopes", sa.Text, nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("provider", "user_key", name="uq_oauth_provider_user"),
    )


def downgrade() -> None:
    op.drop_table("oauth_credentials")
    op.drop_table("audit_logs")
    op.drop_table("google_tasks_sync")
    op.drop_table("google_sheets_sync")
    op.drop_table("meetings")
    op.drop_table("tasks")
    op.drop_table("action_drafts")
    op.drop_table("intent_inferences")
    op.drop_table("processed_slack_events")
    op.drop_table("context_snapshots")
    op.drop_index("ix_slack_messages_thread", "slack_messages")
    op.drop_table("slack_messages")
    op.drop_table("slack_conversations")
    for enum_name in (
        "sync_status",
        "meeting_status",
        "task_status",
        "task_priority",
        "action_draft_state",
        "intent_type",
    ):
        sa.Enum(name=enum_name).drop(op.get_bind(), checkfirst=True)
