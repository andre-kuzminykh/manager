"""FR-CR-05-85 — `ops/telegram_digest.py` cron CLI registry.

The operator runs Telegram digests via cron:

    python -m ops.telegram_digest --type evening-status-report
    python -m ops.telegram_digest --type morning-task-cards

Both flows are operator-critical (FR-CR-05-83 / FR-CR-05-84).
A typo in the registry, a renamed function, or a missing wiring
silently breaks the cron — operators don't notice until the next
day's digest fails to land. These tests pin the contract:

  - both subtypes are present in `_TYPES`
  - they map to the right module-level functions
  - the LLM backend is built ONLY for `evening-status-report`
    (the morning flow renders a deterministic card body and
    doesn't need a backend)
  - `--date` parses to ISO and routes through to the called
    function as `today=`
"""
from __future__ import annotations

from datetime import date

import pytest


def test_cron_registry_includes_evening_and_morning_flows():
    """FR-CR-05-83 / FR-CR-05-84 — both flows must be reachable
    from the cron CLI. A missing entry would silently break the
    next-day digest."""
    from ops.telegram_digest import _TYPES

    assert "evening-status-report" in _TYPES
    assert "morning-task-cards" in _TYPES


def test_cron_registry_evening_routes_to_send_evening_status_report():
    """The cron's `--type evening-status-report` must call the
    `send_evening_status_report` function from `app.telegram_bot.
    evening_status` (NOT one of the legacy `send_evening_plan` /
    `send_morning_digest` notification functions)."""
    from app.telegram_bot.evening_status import send_evening_status_report
    from ops.telegram_digest import _TYPES

    assert _TYPES["evening-status-report"] is send_evening_status_report


def test_cron_registry_morning_routes_to_send_morning_task_cards():
    """The cron's `--type morning-task-cards` must call
    `send_morning_task_cards` from `app.telegram_bot.morning_
    cards`. The legacy `plan-morning` subtype stays wired
    elsewhere — this test pins the modern one."""
    from app.telegram_bot.morning_cards import send_morning_task_cards
    from ops.telegram_digest import _TYPES

    assert _TYPES["morning-task-cards"] is send_morning_task_cards


def test_cron_registry_lists_all_expected_subtypes():
    """Pin the full set of supported subtypes so accidental
    renames/removals trip a test instead of silently breaking
    the operator's cron."""
    from ops.telegram_digest import _TYPES

    expected = {
        "morning-digest",
        "plan-evening",
        "plan-morning",
        "weekly",
        "deadlines",
        "starts-now",
        "thread-reminders",
        "admin-watchlist",
        "evening-status-report",
        "morning-task-cards",
    }
    assert set(_TYPES.keys()) == expected


def test_cron_main_passes_iso_date_through_to_evening(monkeypatch):
    """`--type evening-status-report --date 2026-04-30` must
    invoke `send_evening_status_report` with `today=date(2026,
    4, 30)`. Pin this so a refactor of the kwarg-routing in
    `main()` doesn't silently drop the override (which would
    make replay/back-fill commands write today's audit row
    instead of the requested date)."""
    import ops.telegram_digest as mod

    captured: dict = {}

    def _stub(session, **kwargs):
        captured.update(kwargs)
        from app.telegram_bot.evening_status import EveningStatusReport
        return EveningStatusReport()

    monkeypatch.setitem(mod._TYPES, "evening-status-report", _stub)
    monkeypatch.setattr(mod, "session_scope", _fake_session_scope)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "0:fake")
    monkeypatch.setattr(mod.sys, "argv", [
        "ops.telegram_digest", "--type", "evening-status-report",
        "--date", "2026-04-30",
    ])
    # The settings cache reads TELEGRAM_BOT_TOKEN — clear so the
    # env var lands.
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        rc = mod.main()
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]
    assert rc == 0
    assert captured.get("today") == date(2026, 4, 30)
    # `plan_date` is reserved for legacy plan-evening / plan-morning
    # routes; evening-status-report uses `today=`.
    assert "plan_date" not in captured


def test_cron_main_passes_iso_date_through_to_morning(monkeypatch):
    """`--type morning-task-cards --date 2026-04-30` must
    invoke `send_morning_task_cards` with `today=date(2026, 4,
    30)`. Same contract as the evening route."""
    import ops.telegram_digest as mod

    captured: dict = {}

    def _stub(session, **kwargs):
        captured.update(kwargs)
        from app.telegram_bot.morning_cards import MorningCardsReport
        return MorningCardsReport()

    monkeypatch.setitem(mod._TYPES, "morning-task-cards", _stub)
    monkeypatch.setattr(mod, "session_scope", _fake_session_scope)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "0:fake")
    monkeypatch.setattr(mod.sys, "argv", [
        "ops.telegram_digest", "--type", "morning-task-cards",
        "--date", "2026-04-30",
    ])
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        rc = mod.main()
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]
    assert rc == 0
    assert captured.get("today") == date(2026, 4, 30)
    # The morning flow must NOT pull an LLM backend; only the
    # evening flow does.
    assert "llm" not in captured


def test_cron_main_only_builds_llm_backend_for_evening_flow(monkeypatch):
    """The LLM backend setup is gated on
    `args.type == 'evening-status-report'`. Other flows must NOT
    touch `_build_llm_backend` (the morning flow doesn't need a
    narrative generator)."""
    import ops.telegram_digest as mod

    calls = {"build_llm": 0}

    def _stub_morning(session, **kwargs):
        from app.telegram_bot.morning_cards import MorningCardsReport
        return MorningCardsReport()

    def _build(*a, **kw):  # pragma: no cover — should not be called
        calls["build_llm"] += 1
        return None

    monkeypatch.setitem(mod._TYPES, "morning-task-cards", _stub_morning)
    monkeypatch.setattr(mod, "session_scope", _fake_session_scope)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "0:fake")
    # Patch the lazy import path the evening branch uses.
    import ops.telegram_ingest

    monkeypatch.setattr(ops.telegram_ingest, "_build_llm_backend", _build)
    monkeypatch.setattr(mod.sys, "argv", [
        "ops.telegram_digest", "--type", "morning-task-cards",
    ])
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        mod.main()
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]
    assert calls["build_llm"] == 0


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_session_scope():
    return _FakeSession()
