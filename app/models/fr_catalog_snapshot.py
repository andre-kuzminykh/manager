"""FR-EC-CRITIC-2 — local replica of Viktor's fundraising CRM catalog.

Append-only snapshots of the raw `humanoid_fr_search(query="", limit=2000)` MCP
dump. The resolver reads the latest snapshot instead of hitting the live MCP per
meeting; a daily refresh (lazy-on-access or `ops.sync_fr_catalog`) writes a new
row. This gives independence from the live MCP (a meeting still resolves if his
n8n is down — we use the last good snapshot) and a stable catalog between
refreshes. Additive satellite table.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class FrCatalogSnapshot(Base):
    __tablename__ = "fr_catalog_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)  # raw MCP dump
    entity_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = ["FrCatalogSnapshot"]
