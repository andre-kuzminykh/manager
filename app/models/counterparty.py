"""FR-CR-05-124 — counterparties hub + satellite.

`Counterparty` is the IDENTITY (hub): one row per real
company / fund / investor / partner. `name_normalised` is a
lowercase + ascii-folded form for fast fuzzy lookup against
speech-recognition transcripts.

`CounterpartyAttribute` is the descriptive payload (satellite):
per-source JSON blob of all sheet columns we captured. Same
hub can have multiple satellites — one per source sheet/tab.

Wipe-and-replace sync: `app/sync/counterparties.py` wipes both
tables and reloads from the configured Google Sheets on every
full pull. `counterparty_attrs.counterparty_id` cascades on
delete so wiping the hub is a one-statement operation.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Counterparty(Base, TimestampMixin):
    """Hub: identity row for one counterparty."""

    __tablename__ = "counterparties"
    __table_args__ = (
        UniqueConstraint(
            "name_normalised", "type",
            name="uq_counterparties_name_norm_type",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    name_normalised: Mapped[str] = mapped_column(
        String(512), nullable=False, index=True
    )

    attributes: Mapped[list["CounterpartyAttribute"]] = relationship(
        "CounterpartyAttribute",
        back_populates="counterparty",
        cascade="all, delete-orphan",
    )


class CounterpartyAttribute(Base):
    """Satellite: per-source JSON description of a counterparty."""

    __tablename__ = "counterparty_attrs"
    __table_args__ = (
        UniqueConstraint(
            "counterparty_id", "source",
            name="uq_counterparty_attrs_id_source",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    counterparty_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("counterparties.id", ondelete="CASCADE"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False
    )
    captured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    counterparty: Mapped[Counterparty] = relationship(
        "Counterparty", back_populates="attributes"
    )


__all__ = ["Counterparty", "CounterpartyAttribute"]
