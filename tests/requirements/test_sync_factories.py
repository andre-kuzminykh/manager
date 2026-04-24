"""Sync factories smoke tests.

The Google Sheets / Google Tasks wiring is lazy: build_*_factory
returns None when the relevant setting is empty, and the returned
factory returns None when no service-account credentials are stored.
Finalize swallows that gracefully (tested separately). These tests
exercise the branch points."""
from __future__ import annotations

from unittest.mock import patch

from app.config import Settings
from app.sync.factories import (
    build_google_tasks_factory,
    build_sheets_factory,
)


def test_sheets_factory_disabled_when_spreadsheet_id_empty():
    settings = Settings(GOOGLE_SHEETS_SPREADSHEET_ID="")
    assert build_sheets_factory(settings) is None


def test_sheets_factory_returns_none_when_no_credentials():
    settings = Settings(GOOGLE_SHEETS_SPREADSHEET_ID="spreadsheet-123")
    factory = build_sheets_factory(settings)
    assert factory is not None
    with patch(
        "app.sync.factories._load_service_credentials", return_value=None
    ):
        assert factory() is None


def test_sheets_factory_builds_service_when_credentials_present():
    settings = Settings(GOOGLE_SHEETS_SPREADSHEET_ID="spreadsheet-123")
    factory = build_sheets_factory(settings)
    assert factory is not None

    class _StubCreds:
        pass

    with patch(
        "app.sync.factories._load_service_credentials", return_value=_StubCreds()
    ), patch("app.sync.factories.SheetsSyncService") as MockService:
        factory()
    MockService.assert_called_once()
    kwargs = MockService.call_args.kwargs
    assert kwargs["spreadsheet_id"] == "spreadsheet-123"


def test_gtasks_factory_disabled_when_tasklist_empty():
    settings = Settings(GOOGLE_TASKS_DEFAULT_TASKLIST_ID="")
    assert build_google_tasks_factory(settings) is None


def test_gtasks_factory_returns_none_when_no_credentials():
    settings = Settings(GOOGLE_TASKS_DEFAULT_TASKLIST_ID="@default")
    factory = build_google_tasks_factory(settings)
    assert factory is not None
    with patch(
        "app.sync.factories._load_service_credentials", return_value=None
    ):
        assert factory() is None


def test_gtasks_factory_builds_service_when_credentials_present():
    settings = Settings(GOOGLE_TASKS_DEFAULT_TASKLIST_ID="@default")
    factory = build_google_tasks_factory(settings)
    assert factory is not None

    class _StubCreds:
        pass

    with patch(
        "app.sync.factories._load_service_credentials", return_value=_StubCreds()
    ), patch("app.sync.factories.GoogleTasksSyncService") as MockService:
        factory()
    MockService.assert_called_once()
    kwargs = MockService.call_args.kwargs
    assert kwargs["tasklist_id"] == "@default"


def test_load_service_credentials_returns_none_without_key(monkeypatch):
    """TokenCipher() raises RuntimeError when SECRETS_ENCRYPTION_KEY is
    empty; _load_service_credentials catches and returns None so sync
    is silently disabled."""
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", "")
    from app.config import get_settings
    from app.sync.factories import _load_service_credentials

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        assert _load_service_credentials() is None
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]
