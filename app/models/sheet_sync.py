"""ORM models for the isolated Google Sheets Versioned Sync feature.

Mirrors alembic 0036 (gs_* tables). App-generated string UUID PKs (no pg
extension). Kept in a dedicated module and intentionally NOT re-exported from
app.models.__init__ — the sync engine/runner import from here directly so the
feature stays isolated from the rest of the model registry surface.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class GsSheetIntegration(Base):
    __tablename__ = "gs_sheet_integrations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False, default="task")
    spreadsheet_id: Mapped[str] = mapped_column(Text, nullable=False)
    spreadsheet_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    sheet_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sheet_title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    sync_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    last_sync_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class GsRecord(Base):
    __tablename__ = "gs_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    integration_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("gs_sheet_integrations.id", ondelete="CASCADE"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False, default="task")
    business_key: Mapped[str] = mapped_column(Text, nullable=False)
    current_state_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class GsRecordState(Base):
    __tablename__ = "gs_record_states"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    record_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("gs_records.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    payload_hash: Mapped[str] = mapped_column(Text, nullable=False)
    previous_state_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    rolled_back_to_state_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    sync_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    actor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GsSyncRun(Base):
    __tablename__ = "gs_sync_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    integration_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("gs_sheet_integrations.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rows_read: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deleted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unchanged_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class GsSyncError(Base):
    __tablename__ = "gs_sync_errors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    sync_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("gs_sync_runs.id", ondelete="CASCADE"), nullable=False
    )
    integration_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("gs_sheet_integrations.id", ondelete="CASCADE"), nullable=False
    )
    row_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    business_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_type: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    raw_row: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GsSheetSnapshot(Base):
    __tablename__ = "gs_sheet_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    integration_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("gs_sheet_integrations.id", ondelete="CASCADE"), nullable=False
    )
    sync_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("gs_sync_runs.id", ondelete="CASCADE"), nullable=False
    )
    business_key: Mapped[str] = mapped_column(Text, nullable=False)
    row_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    row_hash: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GsTaskRowMapping(Base):
    __tablename__ = "gs_task_row_mappings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    integration_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("gs_sheet_integrations.id", ondelete="CASCADE"), nullable=False
    )
    spreadsheet_id: Mapped[str] = mapped_column(Text, nullable=False)
    sheet_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sheet_title: Mapped[str] = mapped_column(Text, nullable=False)
    row_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    internal_row_uuid: Mapped[str] = mapped_column(Text, nullable=False)
    record_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("gs_records.id", ondelete="CASCADE"), nullable=False
    )
    last_task_signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_payload_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class GsTaskConfig(Base):
    __tablename__ = "gs_task_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    integration_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("gs_sheet_integrations.id", ondelete="CASCADE"), nullable=False
    )
    max_active_rows_per_tab: Mapped[int] = mapped_column(Integer, nullable=False, default=5000)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    auto_create_overflow_tabs: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    write_sync_status_to_sheet: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


__all__ = [
    "GsSheetIntegration",
    "GsRecord",
    "GsRecordState",
    "GsSyncRun",
    "GsSyncError",
    "GsSheetSnapshot",
    "GsTaskRowMapping",
    "GsTaskConfig",
]
