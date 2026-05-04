"""FR-CR-05-144 — One-shot CLI for the Google Calendar OAuth
consent flow. Stores the resulting refresh token under DB
user_key=`_calendar` (separate from the Sheets/Docs/Tasks one).

Why a separate OAuth client: operator-pinned «новый dал» — wants
Calendar on its own client so adding/removing the Calendar scope
doesn't disrupt existing Sheets/Docs consent.

Usage (manual paste-back flow, no SSH tunnel needed):

    GOOGLE_CALENDAR_CLIENT_ID=... \\
    GOOGLE_CALENDAR_CLIENT_SECRET=... \\
    SECRETS_ENCRYPTION_KEY=... \\
    DATABASE_URL=... \\
    python -m ops.bootstrap_calendar_oauth

The script:
  1. Prints an authorization URL — operator opens it in any
     browser on any machine.
  2. Google asks for consent → redirects to
     `http://localhost:8080/?code=...&state=...&scope=...`
     (browser will fail to connect — that's expected; we only
     need the URL itself).
  3. Operator copies the FULL redirected URL from the browser's
     address bar and pastes it back into the SSH terminal.
  4. The script extracts the code, exchanges it for tokens,
     stores encrypted refresh_token in DB.
"""
from __future__ import annotations

import sys
from datetime import timezone

from google_auth_oauthlib.flow import Flow

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
            "redirect_uris": ["http://localhost:8080/"],
        }
    }

    flow = Flow.from_client_config(
        client_config,
        scopes=GOOGLE_SCOPES_CALENDAR,
        redirect_uri="http://localhost:8080/",
    )
    auth_url, _state = flow.authorization_url(
        access_type="offline", prompt="consent",
        include_granted_scopes="false",
    )
    print(
        "\n=== STEP 1 ===\n"
        "Open this URL in any browser, sign in as Artem, "
        "click 'Allow':\n\n"
        f"{auth_url}\n"
    )
    print(
        "=== STEP 2 ===\n"
        "Google will redirect to http://localhost:8080/?code=...\n"
        "Browser will show 'connection refused' / 'unable to "
        "connect' — that's fine.\n"
        "Copy the FULL redirected URL from the browser's address "
        "bar and paste it below.\n"
    )
    redirected_url = input("Paste redirect URL: ").strip()
    if not redirected_url.startswith("http"):
        print(
            "ERROR: expected a URL starting with http://. Got: "
            f"{redirected_url[:80]}",
            file=sys.stderr,
        )
        return 4

    try:
        flow.fetch_token(authorization_response=redirected_url)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: token exchange failed: {e}", file=sys.stderr)
        return 5

    credentials = flow.credentials
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
        "\n✓ OK: stored encrypted Calendar credentials under "
        f"'{GOOGLE_USER_KEY_CALENDAR}'.\n"
        f"Scopes: {' '.join(credentials.scopes or GOOGLE_SCOPES_CALENDAR)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
