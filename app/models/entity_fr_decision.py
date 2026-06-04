"""FR-EC-CRITIC — append-only audit log of FR entity-resolver decisions.

Every decision (shadow or applied) is recorded so we can compare against
ground truth, calibrate ENTITY_FR_MIN_CONFIDENCE, and roll back forms.
Additive satellite table (SPEC_ENTITY_CRITIC_v0.1 §11).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class EntityFrDecision(Base):
    __tablename__ = "entity_fr_decisions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)   # zoom | fireflies
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str | None] = mapped_column(String(16), nullable=True)  # company | person | team
    mention: Mapped[str] = mapped_column(Text, nullable=False)
    canonical: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_list: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    shadow: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = ["EntityFrDecision"]
