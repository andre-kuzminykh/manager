"""FR-CR-05-175 — one-shot extended-scope re-consent for the
operator's Google account. The Calendar OAuth row currently has
only `calendar.readonly`; we need Docs + Drive (+ Sheets + Tasks)
for the full pipeline to write summary Docs.

This script re-runs the Calendar OAuth flow with the FULL scope set
and stores the result under `_calendar` (same row). After one
re-consent, `_calendar` carries all needed scopes; the FR-CR-05-175
fallback in `app/sync/factories.py` will use it for Docs / Sheets
/ Tasks alongside Calendar.

Prereq: in Google Cloud Console → OAuth consent screen, add the
following scopes to the Calendar OAuth app:
  - https://www.googleapis.com/auth/documents
  - https://www.googleapis.com/auth/drive
  - https://www.googleapis.com/auth/spreadsheets
  - https://www.googleapis.com/auth/tasks

Usage:
    docker compose exec -T bot python -m ops.bootstrap_calendar_oauth_extended

Same paste-back flow as `bootstrap_calendar_oauth.py`.
"""
from __future__ import annotations

import os
import sys
from datetime import timezone

# Allow http://localhost redirect (RFC 8252).
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

from google_auth_oauthlib.flow import Flow

from app.config import get_settings
from app.db import session_scope
from app.sync.docs import GOOGLE_SCOPES_DOCS
from app.sync.google_auth import (
    GOOGLE_SCOPES_CALENDAR,
    GOOGLE_SCOPES_SHEETS,
    GOOGLE_SCOPES_TASKS,
    GOOGLE_USER_KEY_CALENDAR,
    GoogleCredentialStore,
    TokenCipher,
)


_EXTENDED_SCOPES = (
    GOOGLE_SCOPES_CALENDAR
    + GOOGLE_SCOPES_DOCS
    + GOOGLE_SCOPES_SHEETS
    + GOOGLE_SCOPES_TASKS
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
        scopes=_EXTENDED_SCOPES,
        redirect_uri="http://localhost:8080/",
    )
    auth_url, _state = flow.authorization_url(
        access_type="offline", prompt="consent",
        include_granted_scopes="false",
    )
    print(
        "\n=== STEP 1 ===\n"
        f"Requesting scopes:\n  {chr(10) + '  '.join(_EXTENDED_SCOPES)}\n\n"
        "Open this URL in any browser, sign in as the operator, "
        "click 'Allow':\n\n"
        f"{auth_url}\n"
    )
    print(
        "=== STEP 2 ===\n"
        "Google will redirect to http://localhost:8080/?code=...\n"
        "Browser will show 'connection refused' — that's expected.\n"
        "Copy the FULL redirected URL from the address bar and paste "
        "below.\n"
    )
    redirected_url = input("Paste redirect URL: ").strip()
    if not redirected_url.startswith("http"):
        print(
            f"ERROR: expected URL starting with http://. Got: "
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
            "ERROR: Google did not return a refresh_token. Revoke "
            "previous consent at https://myaccount.google.com/permissions "
            "and rerun.",
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
            scopes=list(credentials.scopes or _EXTENDED_SCOPES),
            expires_at=expiry,
        )
    print(
        f"\n✓ OK: stored encrypted credentials under "
        f"'{GOOGLE_USER_KEY_CALENDAR}'.\n"
        f"Scopes granted: {' '.join(credentials.scopes or _EXTENDED_SCOPES)}"
    )
    print(
        "\nNext: docs / sheets / tasks calls should now work via the "
        "FR-CR-05-175 fallback in app/sync/factories.py. Verify with:\n\n"
        "  python -c 'from app.sync.factories import _resolve_credentials; "
        "from app.sync.docs import GOOGLE_SCOPES_DOCS; "
        "c = _resolve_credentials(GOOGLE_SCOPES_DOCS); "
        "print(c.scopes if c else None)'\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
