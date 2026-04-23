from datetime import datetime

from sqlalchemy import DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class OAuthCredential(Base, TimestampMixin):
    """Encrypted OAuth tokens for Google APIs (per user/provider)."""

    __tablename__ = "oauth_credentials"
    __table_args__ = (UniqueConstraint("provider", "user_key", name="uq_oauth_provider_user"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)  # e.g. "google"
    user_key: Mapped[str] = mapped_column(String(128), nullable=False)  # slack user id or email
    access_token_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    scopes: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
