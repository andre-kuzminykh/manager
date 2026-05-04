"""FR-CR-05-144 — One-shot CLI for the Google Calendar OAuth
consent flow. Stores the resulting refresh token under DB
user_key=`_calendar` (separate from the Sheets/Docs/Tasks one).

Why a separate OAuth client: operator-pinned «новый dал» — wants
Calendar on its own client so adding/removing the Calendar scope
doesn't disrupt existing Sheets/Docs consent.

Usage (must run on a machine with a browser):

    GOOGLE_CALENDAR_CLIENT_ID=... \\
    GOOGLE_CALENDAR_CLIENT_SECRET=... \\
    SECRETS_ENCRYPTION_KEY=... \\
    DATABASE_URL=... \\
    python -m ops.bootstrap_calendar_oauth

Headless server flow: forward port via SSH or use any browser
on a machine that can reach `localhost:<random>` of the server.
"""
from __future__ import annotations

import sys
from datetime import timezone

from google_auth_oauthlib.flow import InstalledAppFlow

from app.config import get_settings
from app.db import session_scope
from app.sync.google_auth import (
    GOOGLE_SCOPES_CALENDAR,
    GOOGLE_USER_KEY_CALENDAR,
    GoogleCredentialStore,
    TokenCipher,
)


def main() -> int:
    settings = get_settings()
    if not (
        settings.google_calendar_client_id
        and settings.google_calendar_client_secret
    ):
        print(
            "ERROR: set GOOGLE_CALENDAR_CLIENT_ID and "
            "GOOGLE_CALENDAR_CLIENT_SECRET first",
            file=sys.stderr,
        )
        return 2

    client_config = {
        "installed": {
            "client_id": settings.google_calendar_client_id,
            "client_secret": settings.google_calendar_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(
        client_config, GOOGLE_SCOPES_CALENDAR,
    )
    # FR-CR-05-144 — bind to a fixed port (8080) on 0.0.0.0 so a
    # headless server can be paired with `ssh -L 8080:localhost:
    # 8080 …` from the operator's laptop. Port 0 (random) breaks
    # SSH port-forward setups.
    credentials = flow.run_local_server(
        host="0.0.0.0", port=8080,
        open_browser=False,
        prompt="consent", access_type="offline",
    )

    if not credentials.refresh_token:
        print(
            "ERROR: Google did not return a refresh_token. "
            "Revoke previous consent at "
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
            user_key=GOOGLE_USER_KEY_CALENDAR,
            access_token=credentials.token,
            refresh_token=credentials.refresh_token,
            scopes=list(credentials.scopes or GOOGLE_SCOPES_CALENDAR),
            expires_at=expiry,
        )
    print(
        f"OK: stored encrypted Calendar credentials under "
        f"'{GOOGLE_USER_KEY_CALENDAR}'. Scopes: "
        f"{' '.join(credentials.scopes or GOOGLE_SCOPES_CALENDAR)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
