"""FR-SS-ID — bridge mapping row_uuid <-> task_id for the bidirectional
Sheet sync (SPEC_SHEET_SYNC_v0.1 §2).

Minimal, dedicated link table. Reuses System B's DeveloperMetadata identity
mechanism (`gs_row_uuid`, stamped via `app/sheet_sync/sheets_client`) but is
decoupled from System B's GsRecord/integration state machine — task history &
rollback live in `task_status_events` (S0), not here. Additive satellite.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SheetTaskLink(Base):
    __tablename__ = "sheet_task_links"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    spreadsheet_id: Mapped[str] = mapped_column(Text, nullable=False)
    sheet_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # DeveloperMetadata gs_row_uuid — survives sort / insert / delete
    row_uuid: Mapped[str] = mapped_column(String(64), nullable=False)
    # last known 1-based row number (advisory only; uuid is the truth)
    row_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # hash of the row payload at last sync — anti-stale / "did the human edit"
    last_payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # one row per task per spreadsheet; uuid unique within a spreadsheet
        UniqueConstraint("spreadsheet_id", "task_id", name="uq_sheet_task_links_ss_task"),
        UniqueConstraint("spreadsheet_id", "row_uuid", name="uq_sheet_task_links_ss_uuid"),
    )


__all__ = ["SheetTaskLink"]
