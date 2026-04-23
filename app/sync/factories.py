"""Factories that build sync services lazily on each finalize attempt.

We deliberately build a fresh service per call so token refreshes and new
credentials are picked up without restarting the process.
"""
from __future__ import annotations

from typing import Callable

from app.config import Settings
from app.db import session_scope
from app.logging_setup import get_logger
from app.sync.google_auth import (
    GoogleCredentialStore,
    TokenCipher,
    build_google_credentials,
)
from app.sync.sheets import SheetsSyncService
from app.sync.tasks_api import GoogleTasksSyncService

log = get_logger(__name__)

_SERVICE_ACCOUNT_USER_KEY = "_service_account"


def _load_service_credentials():
    try:
        cipher = TokenCipher()
    except RuntimeError as e:
        log.info("google_sync_disabled_no_encryption_key", reason=str(e))
        return None
    store = GoogleCredentialStore(cipher)
    with session_scope() as session:
        record = store.load(session, user_key=_SERVICE_ACCOUNT_USER_KEY)
        if record is None:
            log.info("google_sync_disabled_no_credentials")
            return None
        return build_google_credentials(record, store)


def build_sheets_factory(settings: Settings) -> Callable[[], SheetsSyncService | None] | None:
    if not settings.google_sheets_spreadsheet_id:
        return None

    def factory() -> SheetsSyncService | None:
        creds = _load_service_credentials()
        if creds is None:
            return None
        return SheetsSyncService(
            credentials=creds,
            spreadsheet_id=settings.google_sheets_spreadsheet_id,
        )

    return factory


def build_google_tasks_factory(
    settings: Settings,
) -> Callable[[], GoogleTasksSyncService | None] | None:
    if not settings.google_tasks_default_tasklist_id:
        return None

    def factory() -> GoogleTasksSyncService | None:
        creds = _load_service_credentials()
        if creds is None:
            return None
        return GoogleTasksSyncService(
            credentials=creds,
            tasklist_id=settings.google_tasks_default_tasklist_id,
        )

    return factory
