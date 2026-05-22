"""FR-CR-05-193e — entity_resolution_cache SQLAlchemy model."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB

from app.models.base import Base


DEFAULT_TTL_DAYS = 7


class EntityResolutionCache(Base):
    """Cache for Step 2 matcher LLM results.

    Key = sha256(text + known_people_hash + known_orgs_hash).
    Payload = full matcher response dict.
    TTL default 7 days.
    """
    __tablename__ = "entity_resolution_cache"

    cache_key = Column(String(64), primary_key=True)
    payload = Column(JSONB, nullable=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now(),
    )
    expires_at = Column(DateTime(timezone=True), nullable=False)
    hits_count = Column(Integer, nullable=False, server_default="0")

    def __repr__(self) -> str:
        return (
            f"<EntityResolutionCache key={self.cache_key[:8]}... "
            f"expires_at={self.expires_at} hits={self.hits_count}>"
        )
