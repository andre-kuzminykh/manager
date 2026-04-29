"""Factories that build sync services lazily on each sync attempt.

We deliberately build a fresh service per call so token refreshes and new
credentials are picked up without restarting the process.

Two credential sources are supported, in priority order:

1. **Service Account** (recommended) — a single JSON key in either
   ``GOOGLE_SERVICE_ACCOUNT_JSON`` or ``GOOGLE_SERVICE_ACCOUNT_JSON_PATH``.
   Share the spreadsheet with the service-account email as *Editor*. No
   browser flow, no token refresh — the bot acts as a tech account.
2. **OAuth user credentials** — legacy path; reads from the
   ``oauth_credentials`` row keyed by ``_service_account`` (the row was
   seeded once via the OAuth flow). Kept for back-compat.
"""
from __future__ import annotations

from typing import Callable

from app.config import Settings
from app.db import session_scope
from app.logging_setup import get_logger
from app.sync.google_auth import (
    GOOGLE_SCOPES_SHEETS,
    GOOGLE_SCOPES_TASKS,
    GoogleCredentialStore,
    TokenCipher,
    build_google_credentials,
    load_service_account_credentials,
)
from app.sync.sheets import SheetsPullService, SheetsSyncService
from app.sync.tasks_api import GoogleTasksSyncService
from app.sync.team_sheet import TeamSheetSync

log = get_logger(__name__)

_SERVICE_ACCOUNT_USER_KEY = "_service_account"


def _load_oauth_credentials():
    try:
        cipher = TokenCipher()
    except RuntimeError as e:
        log.info("google_sync_disabled_no_encryption_key", reason=str(e))
        return None
    store = GoogleCredentialStore(cipher)
    with session_scope() as session:
        record = store.load(session, user_key=_SERVICE_ACCOUNT_USER_KEY)
        if record is None:
            return None
        return build_google_credentials(record, store)


def _resolve_credentials(scopes: list[str]):
    """Service Account first; fall back to OAuth from DB."""
    try:
        sa_creds = load_service_account_credentials(scopes)
    except Exception as e:  # noqa: BLE001
        log.warning("service_account_credentials_invalid", error=str(e))
        sa_creds = None
    if sa_creds is not None:
        return sa_creds
    creds = _load_oauth_credentials()
    if creds is None:
        log.info("google_sync_disabled_no_credentials")
    return creds


def build_sheets_factory(settings: Settings) -> Callable[[], SheetsSyncService | None] | None:
    if not settings.google_sheets_spreadsheet_id:
        return None

    def factory() -> SheetsSyncService | None:
        creds = _resolve_credentials(GOOGLE_SCOPES_SHEETS)
        if creds is None:
            return None
        return SheetsSyncService(
            credentials=creds,
            spreadsheet_id=settings.google_sheets_spreadsheet_id,
            sheet_name=settings.google_sheets_tab_name or "Main",
        )

    return factory


def build_sheets_pull_factory(
    settings: Settings,
) -> Callable[[], SheetsPullService | None] | None:
    """FR-CR-05-11 — factory for the Sheet → DB pull. Same
    spreadsheet + tab as the existing push factory."""
    if not settings.google_sheets_spreadsheet_id:
        return None

    def factory() -> SheetsPullService | None:
        creds = _resolve_credentials(GOOGLE_SCOPES_SHEETS)
        if creds is None:
            return None
        return SheetsPullService(
            credentials=creds,
            spreadsheet_id=settings.google_sheets_spreadsheet_id,
            sheet_name=settings.google_sheets_tab_name or "Main",
        )

    return factory


def build_team_sheet_factory(
    settings: Settings,
) -> Callable[[], TeamSheetSync | None] | None:
    """FR-CR-05-10 — factory for the Team registry sync. Falls back
    to the tasks spreadsheet when ``GOOGLE_TEAM_SHEETS_SPREADSHEET_ID``
    isn't set (single-sheet deploy → just add a `Team` tab)."""
    spreadsheet_id = (
        settings.google_team_sheets_spreadsheet_id
        or settings.google_sheets_spreadsheet_id
    )
    if not spreadsheet_id:
        return None

    def factory() -> TeamSheetSync | None:
        creds = _resolve_credentials(GOOGLE_SCOPES_SHEETS)
        if creds is None:
            return None
        return TeamSheetSync(
            credentials=creds,
            spreadsheet_id=spreadsheet_id,
            sheet_name=settings.google_team_sheets_tab_name or "Team",
        )

    return factory


def build_google_tasks_factory(
    settings: Settings,
) -> Callable[[], GoogleTasksSyncService | None] | None:
    if not settings.google_tasks_default_tasklist_id:
        return None

    def factory() -> GoogleTasksSyncService | None:
        creds = _resolve_credentials(GOOGLE_SCOPES_TASKS)
        if creds is None:
            return None
        return GoogleTasksSyncService(
            credentials=creds,
            tasklist_id=settings.google_tasks_default_tasklist_id,
        )

    return factory
