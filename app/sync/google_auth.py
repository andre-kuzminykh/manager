from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from google.oauth2.credentials import Credentials
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import OAuthCredential

GOOGLE_SCOPES_SHEETS = ["https://www.googleapis.com/auth/spreadsheets"]
GOOGLE_SCOPES_TASKS = ["https://www.googleapis.com/auth/tasks"]
# FR-CR-05-144 — read-only Calendar access for the meeting-
# title matcher (Fireflies + Zoom). Operator pinned a separate
# OAuth client for Calendar so Sheets/Docs consent isn't
# affected by re-consent on Calendar scope changes.
GOOGLE_SCOPES_CALENDAR = ["https://www.googleapis.com/auth/calendar.readonly"]

# DB `user_key` keys for the OAuth credential store. Each
# OAuth client (Sheets+Docs+Tasks vs Calendar) gets its own
# row so refresh tokens stay isolated.
GOOGLE_USER_KEY_SERVICE = "_service_account"
GOOGLE_USER_KEY_CALENDAR = "_calendar"


class TokenCipher:
    """Symmetric encryption for OAuth tokens at rest.

    The key comes from SECRETS_ENCRYPTION_KEY (urlsafe base64 32 bytes). If it
    is missing we raise loudly — storing OAuth tokens in plaintext is a bug.
    """

    def __init__(self, key: str | None = None) -> None:
        key = key or get_settings().secrets_encryption_key
        if not key:
            raise RuntimeError(
                "SECRETS_ENCRYPTION_KEY is not set; cannot store OAuth tokens securely."
            )
        try:
            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        except (ValueError, TypeError) as e:
            raise RuntimeError(f"SECRETS_ENCRYPTION_KEY is not a valid Fernet key: {e}") from e

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except InvalidToken as e:
            raise RuntimeError("Failed to decrypt stored OAuth token") from e

    @staticmethod
    def generate_key() -> str:
        return base64.urlsafe_b64encode(Fernet.generate_key()).decode("ascii").rstrip("=")


class GoogleCredentialStore:
    """Load and persist Google OAuth credentials per user_key (Slack user id or email)."""

    def __init__(self, cipher: TokenCipher) -> None:
        self._cipher = cipher

    def save(
        self,
        session: Session,
        *,
        user_key: str,
        access_token: str,
        refresh_token: str | None,
        scopes: list[str],
        expires_at: datetime | None,
    ) -> OAuthCredential:
        record = (
            session.query(OAuthCredential)
            .filter_by(provider="google", user_key=user_key)
            .one_or_none()
        )
        if record is None:
            record = OAuthCredential(provider="google", user_key=user_key)
            session.add(record)
        record.access_token_ciphertext = self._cipher.encrypt(access_token)
        record.refresh_token_ciphertext = (
            self._cipher.encrypt(refresh_token) if refresh_token else None
        )
        record.scopes = " ".join(scopes)
        record.token_expires_at = expires_at
        session.flush()
        return record

    def load(self, session: Session, *, user_key: str) -> OAuthCredential | None:
        return (
            session.query(OAuthCredential)
            .filter_by(provider="google", user_key=user_key)
            .one_or_none()
        )

    def decrypt_access_token(self, record: OAuthCredential) -> str:
        return self._cipher.decrypt(record.access_token_ciphertext)

    def decrypt_refresh_token(self, record: OAuthCredential) -> str | None:
        if not record.refresh_token_ciphertext:
            return None
        return self._cipher.decrypt(record.refresh_token_ciphertext)


def build_google_credentials(
    record: OAuthCredential,
    store: GoogleCredentialStore,
) -> Credentials:
    settings = get_settings()
    scopes = (record.scopes or "").split() or None
    creds = Credentials(
        token=store.decrypt_access_token(record),
        refresh_token=store.decrypt_refresh_token(record),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=scopes,
        expiry=_strip_tz(record.token_expires_at),
    )
    return creds


def build_google_calendar_credentials(
    record: OAuthCredential,
    store: GoogleCredentialStore,
) -> Credentials:
    """FR-CR-05-144 — same as `build_google_credentials` but
    uses the Calendar-specific OAuth client (`GOOGLE_CALENDAR_*`
    settings). Operator-pinned: Calendar runs on its own OAuth
    app so Sheets/Docs consent isn't disrupted when the
    Calendar scope is added or revoked."""
    settings = get_settings()
    scopes = (record.scopes or "").split() or None
    return Credentials(
        token=store.decrypt_access_token(record),
        refresh_token=store.decrypt_refresh_token(record),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_calendar_client_id,
        client_secret=settings.google_calendar_client_secret,
        scopes=scopes,
        expiry=_strip_tz(record.token_expires_at),
    )


def _strip_tz(dt: datetime | None) -> datetime | None:
    # google-auth expects naive UTC datetimes for expiry.
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def credentials_payload(creds: Credentials) -> dict[str, Any]:
    """Extract fields we persist after a refresh."""
    return {
        "access_token": creds.token,
        "refresh_token": creds.refresh_token,
        "expires_at": creds.expiry.replace(tzinfo=timezone.utc) if creds.expiry else None,
        "scopes": list(creds.scopes or []),
    }


def load_service_account_credentials(scopes: list[str]):
    """Build google-auth Service Account credentials from settings.

    Recommended path for production: a single GOOGLE_SERVICE_ACCOUNT_JSON
    env (or _PATH) replaces the entire OAuth dance. Returns ``None`` if
    neither is configured. Raised exceptions bubble up — a misconfigured
    SA is a startup-time bug, not a runtime one.
    """
    import json
    from pathlib import Path

    from google.oauth2 import service_account

    settings = get_settings()
    raw = (settings.google_service_account_json or "").strip()
    path = (settings.google_service_account_json_path or "").strip()

    info: dict[str, Any] | None = None
    if raw:
        info = json.loads(raw)
    elif path:
        info = json.loads(Path(path).read_text(encoding="utf-8"))

    if info is None:
        return None
    return service_account.Credentials.from_service_account_info(info, scopes=scopes)
