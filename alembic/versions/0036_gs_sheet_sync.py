"""FR-GS-* / FR-TASK-* — Google Sheets Versioned Sync: isolated schema.

ADDITIVE ONLY. Creates the `gs_*` family of tables for the new, isolated
Sheets-sync feature. Does NOT touch any existing table (tasks, action_drafts,
records, etc.). All PKs are application-generated TEXT uuids (no pg extension
required). FKs reference only `gs_*` tables — never existing tables — so a bug
in this feature cannot break existing data via constraints.

Revision ID: 0036_gs_sheet_sync
Revises: 0035_ff_duration_minutes
Create Date: 2026-05-25
"""
from __future__ import annotations

from alembic import op

revision = "0036_gs_sheet_sync"
down_revision = "0035_ff_duration_minutes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS gs_sheet_integrations (
          id TEXT PRIMARY KEY,
          entity_type TEXT NOT NULL DEFAULT 'task',
          spreadsheet_id TEXT NOT NULL,
          spreadsheet_title TEXT,
          sheet_id BIGINT NOT NULL,
          sheet_title TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'draft',
          sync_interval_seconds INT NOT NULL DEFAULT 300,
          timezone TEXT NOT NULL DEFAULT 'UTC',
          last_sync_run_id TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (spreadsheet_id, sheet_id)
        );
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS gs_records (
          id TEXT PRIMARY KEY,
          integration_id TEXT NOT NULL REFERENCES gs_sheet_integrations(id) ON DELETE CASCADE,
          entity_type TEXT NOT NULL DEFAULT 'task',
          business_key TEXT NOT NULL,
          current_state_id TEXT,
          deleted_at TIMESTAMPTZ,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (integration_id, business_key)
        );
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS gs_record_states (
          id TEXT PRIMARY KEY,
          record_id TEXT NOT NULL REFERENCES gs_records(id) ON DELETE CASCADE,
          event_type TEXT NOT NULL,
          source TEXT NOT NULL,
          payload JSONB NOT NULL,
          payload_hash TEXT NOT NULL,
          previous_state_id TEXT,
          rolled_back_to_state_id TEXT,
          sync_run_id TEXT,
          actor TEXT,
          reason TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_gs_record_states_record_created "
        "ON gs_record_states (record_id, created_at DESC);"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS gs_sync_runs (
          id TEXT PRIMARY KEY,
          integration_id TEXT NOT NULL REFERENCES gs_sheet_integrations(id) ON DELETE CASCADE,
          status TEXT NOT NULL,
          trigger_type TEXT NOT NULL,
          started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          finished_at TIMESTAMPTZ,
          rows_read INT NOT NULL DEFAULT 0,
          created_count INT NOT NULL DEFAULT 0,
          updated_count INT NOT NULL DEFAULT 0,
          deleted_count INT NOT NULL DEFAULT 0,
          unchanged_count INT NOT NULL DEFAULT 0,
          error_count INT NOT NULL DEFAULT 0,
          error_type TEXT,
          error_message TEXT
        );
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS gs_sync_errors (
          id TEXT PRIMARY KEY,
          sync_run_id TEXT NOT NULL REFERENCES gs_sync_runs(id) ON DELETE CASCADE,
          integration_id TEXT NOT NULL REFERENCES gs_sheet_integrations(id) ON DELETE CASCADE,
          row_number INT,
          business_key TEXT,
          error_type TEXT NOT NULL,
          message TEXT NOT NULL,
          raw_row JSONB,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS gs_sheet_snapshots (
          id TEXT PRIMARY KEY,
          integration_id TEXT NOT NULL REFERENCES gs_sheet_integrations(id) ON DELETE CASCADE,
          sync_run_id TEXT NOT NULL REFERENCES gs_sync_runs(id) ON DELETE CASCADE,
          business_key TEXT NOT NULL,
          row_number INT,
          row_hash TEXT NOT NULL,
          normalized_payload JSONB NOT NULL,
          seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (integration_id, sync_run_id, business_key)
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_gs_snapshots_integration_key_seen "
        "ON gs_sheet_snapshots (integration_id, business_key, seen_at DESC);"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS gs_task_row_mappings (
          id TEXT PRIMARY KEY,
          integration_id TEXT NOT NULL REFERENCES gs_sheet_integrations(id) ON DELETE CASCADE,
          spreadsheet_id TEXT NOT NULL,
          sheet_id BIGINT NOT NULL,
          sheet_title TEXT NOT NULL,
          row_number INT,
          internal_row_uuid TEXT NOT NULL,
          record_id TEXT NOT NULL REFERENCES gs_records(id) ON DELETE CASCADE,
          last_task_signature TEXT,
          last_payload_hash TEXT,
          last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (integration_id, internal_row_uuid)
        );
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS gs_exported_sources (
          id TEXT PRIMARY KEY,
          integration_id TEXT NOT NULL REFERENCES gs_sheet_integrations(id) ON DELETE CASCADE,
          source_kind TEXT NOT NULL,
          source_id TEXT NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (integration_id, source_kind, source_id)
        );
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS gs_task_configs (
          id TEXT PRIMARY KEY,
          integration_id TEXT NOT NULL REFERENCES gs_sheet_integrations(id) ON DELETE CASCADE,
          max_active_rows_per_tab INT NOT NULL DEFAULT 5000,
          timezone TEXT NOT NULL DEFAULT 'UTC',
          auto_create_overflow_tabs BOOLEAN NOT NULL DEFAULT true,
          write_sync_status_to_sheet BOOLEAN NOT NULL DEFAULT false,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (integration_id)
        );
        """
    )


def downgrade() -> None:
    # Reverse dependency order. Drops ONLY gs_* tables.
    for tbl in (
        "gs_exported_sources",
        "gs_task_configs",
        "gs_task_row_mappings",
        "gs_sheet_snapshots",
        "gs_sync_errors",
        "gs_sync_runs",
        "gs_record_states",
        "gs_records",
        "gs_sheet_integrations",
    ):
        op.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE;")
