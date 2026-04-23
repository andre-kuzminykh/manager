"""One-shot CLI that runs the Google OAuth installed-app flow once and stores
the resulting refresh token in the DB under user_key = "_service_account".

The bot then uses these credentials to sync to Sheets and Google Tasks.

Usage:
    python -m ops.bootstrap_google_oauth
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from google_auth_oauthlib.flow import InstalledAppFlow

from app.config import get_settings
from app.db import session_scope
from app.sync.google_auth import (
    GOOGLE_SCOPES_SHEETS,
    GOOGLE_SCOPES_TASKS,
    GoogleCredentialStore,
    TokenCipher,
)

SERVICE_ACCOUNT_USER_KEY = "_service_account"
SCOPES = GOOGLE_SCOPES_SHEETS + GOOGLE_SCOPES_TASKS


def main() -> int:
    settings = get_settings()
    if not settings.google_client_id or not settings.google_client_secret:
        print("ERROR: set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET first", file=sys.stderr)
        return 2

    client_config = {
        "installed": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    # Spins up a short-lived local webserver to receive the auth code.
    credentials = flow.run_local_server(port=0, prompt="consent", access_type="offline")

    if not credentials.refresh_token:
        print(
            "ERROR: Google did not return a refresh_token. Revoke previous consent at "
            "https://myaccount.google.com/permissions and rerun.",
            file=sys.stderr,
        )
        return 3

    cipher = TokenCipher()
    store = GoogleCredentialStore(cipher)
    expiry = (
        credentials.expiry.replace(tzinfo=timezone.utc)
        if credentials.expiry
        else None
    )
    with session_scope() as session:
        store.save(
            session,
            user_key=SERVICE_ACCOUNT_USER_KEY,
            access_token=credentials.token,
            refresh_token=credentials.refresh_token,
            scopes=list(credentials.scopes or SCOPES),
            expires_at=expiry,
        )
    print(
        f"OK: stored encrypted Google credentials for '{SERVICE_ACCOUNT_USER_KEY}'. "
        f"Scopes: {' '.join(credentials.scopes or SCOPES)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
