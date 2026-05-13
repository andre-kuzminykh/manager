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
    GOOGLE_SCOPES_CALENDAR,
    GOOGLE_SCOPES_SHEETS,
    GOOGLE_SCOPES_TASKS,
    GOOGLE_USER_KEY_CALENDAR,
    GoogleCredentialStore,
    TokenCipher,
    build_google_calendar_credentials,
    build_google_credentials,
    load_service_account_credentials,
)
from app.sync.docs import DocsExportService, GOOGLE_SCOPES_DOCS
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


def build_docs_factory(
    settings: Settings,
) -> Callable[[], DocsExportService | None] | None:
    """FR-CR-05-43 — factory for the Google Docs export. Used by
    the Fireflies pipeline to dump the detailed summary into a
    Doc named after the meeting."""
    def factory() -> DocsExportService | None:
        creds = _resolve_credentials(GOOGLE_SCOPES_DOCS)
        if creds is None:
            return None
        return DocsExportService(credentials=creds)

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


def build_counterparties_sheet_factory(
    settings: Settings,
) -> Callable[[], "CounterpartiesSheetSync | None"] | None:
    """FR-CR-05-124 — factory for the counterparties directory
    sync. Returns None when neither sheet is configured (the
    feature is opt-in)."""
    if not (
        settings.counterparties_status_sheet_id
        or settings.counterparties_outreach_sheet_id
        or settings.counterparties_targets_sheet_id
    ):
        return None

    def factory() -> "CounterpartiesSheetSync | None":
        from app.sync.counterparties import CounterpartiesSheetSync

        creds = _resolve_credentials(GOOGLE_SCOPES_SHEETS)
        if creds is None:
            return None

        def _split_tabs(s: str) -> list[str]:
            return [t.strip() for t in (s or "").split(",") if t.strip()]

        name_first_tabs: list[tuple[str, str]] = []
        if settings.counterparties_outreach_sheet_id:
            for tab in _split_tabs(
                settings.counterparties_outreach_tab_names
            ):
                name_first_tabs.append(
                    (settings.counterparties_outreach_sheet_id, tab)
                )
        if settings.counterparties_targets_sheet_id:
            for tab in _split_tabs(
                settings.counterparties_targets_tab_names
            ):
                name_first_tabs.append(
                    (settings.counterparties_targets_sheet_id, tab)
                )
        return CounterpartiesSheetSync(
            credentials=creds,
            status_spreadsheet_id=settings.counterparties_status_sheet_id,
            status_tab_name=settings.counterparties_status_tab_name,
            name_first_tabs=name_first_tabs,
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


def build_google_tasks_pull_factory(
    settings: Settings,
) -> Callable[[], "GoogleTasksPullService | None"] | None:
    """FR-CR-05-61 — pull side of the Google Tasks sync. Same
    credentials + tasklist as the push factory above. Returns
    None when no tasklist id is configured."""
    if not settings.google_tasks_default_tasklist_id:
        return None
    from app.sync.tasks_pull import GoogleTasksPullService

    def factory() -> "GoogleTasksPullService | None":
        creds = _resolve_credentials(GOOGLE_SCOPES_TASKS)
        if creds is None:
            return None
        return GoogleTasksPullService(
            credentials=creds,
            tasklist_id=settings.google_tasks_default_tasklist_id,
        )

    return factory


def build_calendar_credentials_factory(
    settings: Settings,
) -> Callable[[], object | None] | None:
    """FR-CR-05-144 — return a callable that loads + refreshes
    the Calendar OAuth credentials from the DB (separate user
    key from the Sheets/Docs/Tasks one). Returns None when the
    Calendar OAuth client isn't configured.

    The factory is passed into `match_and_format_title` —
    `googleapiclient.discovery.build('calendar', ...)` calls
    `creds.refresh()` automatically when the access token has
    expired, so a single load per pipeline run is enough.
    """
    if not (
        settings.google_calendar_client_id
        and settings.google_calendar_client_secret
    ):
        return None

    def factory():
        try:
            cipher = TokenCipher()
        except RuntimeError as e:
            log.info(
                "calendar_oauth_disabled_no_encryption_key",
                reason=str(e),
            )
            return None
        store = GoogleCredentialStore(cipher)
        with session_scope() as session:
            record = store.load(
                session, user_key=GOOGLE_USER_KEY_CALENDAR,
            )
            if record is None:
                log.info(
                    "calendar_oauth_no_stored_credentials",
                    user_key=GOOGLE_USER_KEY_CALENDAR,
                    hint=(
                        "run `python -m ops.bootstrap_calendar_oauth` "
                        "to do the one-time consent flow"
                    ),
                )
                return None
            return build_google_calendar_credentials(record, store)

    return factory


def build_calendar_sa_credentials_factory(
    settings: Settings,
) -> Callable[[], object | None] | None:
    """FR-CR-05-165 — Service-Account path for Calendar reads.

    When the operator hasn't set up the user-OAuth flow (or its
    refresh token has been invalidated by a Client Secret rotation),
    fall back to a service-account key. The SA must be:

      1. Loadable via ``GOOGLE_SERVICE_ACCOUNT_JSON{,_PATH}``.
      2. Granted **«See all event details»** on the target
         calendar(s) via Google Calendar UI (one-time share — no
         Domain-Wide Delegation required).

    Read-only scope (`calendar.readonly`) is sufficient — the
    agenda pipeline never writes to Calendar.

    Returns None when no SA JSON is configured / the file path
    doesn't exist. Caller (`AgendaRunner`) treats this as «no SA
    fallback available» and continues with OAuth-only.
    """

    def factory():
        try:
            creds = load_service_account_credentials(GOOGLE_SCOPES_CALENDAR)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "calendar_sa_credentials_invalid", error=str(e),
            )
            return None
        if creds is None:
            log.info(
                "calendar_sa_credentials_unavailable",
                hint=(
                    "GOOGLE_SERVICE_ACCOUNT_JSON[_PATH] missing — "
                    "Calendar SA fallback disabled"
                ),
            )
            return None
        return creds

    return factory


def build_calendar_credentials_factory_with_sa_fallback(
    settings: Settings,
) -> Callable[[], object | None] | None:
    """FR-CR-05-165 — Composite factory:

      1. Try user OAuth (FR-CR-05-144). Best when the operator
         has gone through the consent flow.
      2. Fall back to Service Account (`calendar.readonly`) when
         OAuth credentials are missing OR the OAuth refresh fails
         at runtime (e.g. Client Secret rotated, refresh token
         invalidated — operator hasn't re-bootstrapped yet).

    Returns None ONLY when BOTH sources are unavailable.
    """
    oauth_inner = build_calendar_credentials_factory(settings)
    sa_inner = build_calendar_sa_credentials_factory(settings)
    if oauth_inner is None and sa_inner is None:
        return None

    def factory():
        if oauth_inner is not None:
            try:
                creds = oauth_inner()
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "calendar_oauth_load_failed",
                    error=str(e),
                    hint="will try Service Account fallback",
                )
                creds = None
            if creds is not None:
                return creds
        if sa_inner is not None:
            sa_creds = sa_inner()
            if sa_creds is not None:
                log.info("calendar_using_sa_fallback")
                return sa_creds
        return None

    return factory
