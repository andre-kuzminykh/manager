"""Tests for backend.services.ceo_brain.

Service is intended to read from external Postgres (CEO Brain DB).
В CI / тестовом окружении CEO_BRAIN_DB_URL обычно НЕ задан →
service должен gracefully вернуть DEFAULT KPIs с health.configured=False.

Если CEO_BRAIN_DB_URL задан валидно, можно гонять live-tests через
real Postgres (skipped по default через env-flag).
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

from backend.services.ceo_brain import (
    CEOBrainFilters,
    get_daily_tasks_series,
    get_kpis,
    get_recent_meetings,
    make_default_filters,
)


# -- helpers ------------------------------------------------------------------


@pytest.fixture
def no_db_env(monkeypatch):
    """Clear CEO_BRAIN_DB_URL so service returns safe defaults."""
    monkeypatch.delenv("CEO_BRAIN_DB_URL", raising=False)


@pytest.fixture
def filters():
    return make_default_filters(days=7)


# -- DEFAULT path (no DB configured) ------------------------------------------


def test_get_kpis_without_db_returns_defaults(no_db_env, filters):
    """Service must NOT raise when CEO_BRAIN_DB_URL is missing."""
    result = get_kpis(filters)
    assert result["tasks_total"] == 0
    assert result["meetings_zoom_total"] == 0
    assert result["meetings_fireflies_total"] == 0
    assert result["tasks_by_source"] == {}
    assert result["top_assignees"] == []
    assert result["health"]["configured"] is False
    assert "CEO_BRAIN_DB_URL" in result["health"]["reason"]


def test_get_recent_meetings_without_db_returns_empty(no_db_env, filters):
    result = get_recent_meetings(filters)
    assert result == []


def test_get_daily_tasks_series_without_db_returns_empty(no_db_env, filters):
    result = get_daily_tasks_series(filters)
    assert result == []


# -- FILTERS ------------------------------------------------------------------


def test_make_default_filters_default_7_days():
    f = make_default_filters()
    assert f.end_date == date.today()
    assert f.start_date == date.today() - timedelta(days=6)
    assert f.source is None


def test_make_default_filters_custom_days():
    f = make_default_filters(days=30)
    assert (f.end_date - f.start_date).days == 29


def test_filters_start_end_dt_tzaware(filters):
    assert filters.start_dt.tzinfo is not None
    assert filters.end_dt.tzinfo is not None
    assert filters.start_dt < filters.end_dt


def test_filters_source_normalized():
    f = CEOBrainFilters(
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 7),
        source="zoom",
    )
    assert f.source == "zoom"


# -- DB CONNECTION FAILURE ----------------------------------------------------


def test_connect_failure_returns_defaults(monkeypatch, filters):
    """Bad DSN → connect raises → service returns defaults, no propagation."""
    monkeypatch.setenv("CEO_BRAIN_DB_URL", "postgresql://no-such-host:1/none")
    # The mock makes psycopg.connect raise OperationalError-style:
    with patch("psycopg.connect", side_effect=Exception("conn refused")):
        result = get_kpis(filters)
    assert result["tasks_total"] == 0
    assert result["health"]["configured"] is False


# -- Query path via mocked connection (no real Postgres) ----------------------


def _mock_conn_with_results(cursor_results: list[list]):
    """Build a context-manager-aware mock connection.

    `cursor_results` is a list of result-lists, one per `cur.execute()` call.
    Each result-list is what `cur.fetchall()` returns (or `[result_for_fetchone]`
    if the query uses fetchone).
    """
    cur = MagicMock()
    cur.fetchall.side_effect = cursor_results
    cur.fetchone.side_effect = [r[0] if r else None for r in cursor_results]

    # `with conn.cursor() as cur` support
    cur_ctx = MagicMock()
    cur_ctx.__enter__.return_value = cur
    cur_ctx.__exit__.return_value = False

    conn = MagicMock()
    conn.cursor.return_value = cur_ctx
    # `with conn:` support (transaction manager)
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    return conn, cur


def test_get_kpis_with_mocked_db(monkeypatch, filters):
    """Smoke-test the SQL pipeline by mocking psycopg.connect."""
    monkeypatch.setenv("CEO_BRAIN_DB_URL", "postgresql://fake")
    # Queries fired in order (see ceo_brain.get_kpis):
    # 1. tasks_by_source → fetchall
    # 2. tasks_by_status → fetchall
    # 3. tasks_by_priority → fetchall
    # 4. top_assignees → fetchall
    # 5. zoom meetings → fetchone (count, summarised)
    # 6. fireflies meetings → fetchone
    # 7. processed_telegram_messages → fetchone (total, task_extracted)
    # 8-11. health indicators → fetchone × 4
    cur = MagicMock()
    cur.fetchall.side_effect = [
        [("telegram", 12), ("zoom", 5), ("fireflies", 3)],  # tasks_by_source
        [("todo", 8), ("done", 12)],                          # tasks_by_status
        [("medium", 15), ("high", 5)],                        # tasks_by_priority
        [("Алина", 7), ("Дима", 5), ("Артем", 3)],            # top_assignees
    ]
    cur.fetchone.side_effect = [
        (5, 4),                                  # zoom: total, summarised
        (3, 3),                                  # fireflies: total, summarised
        (120, 20),                               # processed_telegram_messages
        (1,),                                    # zoom_orphans
        (0,),                                    # fireflies_orphans
        (None,),                                 # zoom_last_processed_at
        (None,),                                 # fireflies_last_processed_at
    ]
    cur_ctx = MagicMock()
    cur_ctx.__enter__.return_value = cur
    cur_ctx.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cur_ctx
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False

    with patch("psycopg.connect", return_value=conn):
        result = get_kpis(filters)

    assert result["tasks_total"] == 12 + 5 + 3
    assert result["tasks_by_source"] == {"telegram": 12, "zoom": 5, "fireflies": 3}
    assert result["meetings_zoom_total"] == 5
    assert result["meetings_zoom_summarised"] == 4
    assert result["meetings_fireflies_total"] == 3
    assert result["meetings_fireflies_summarised"] == 3
    assert result["tg_messages_total"] == 120
    assert result["tg_messages_classified_as_task"] == 20
    assert result["top_assignees"][0]["assignee"] == "Алина"
    assert result["top_assignees"][0]["tasks"] == 7
    assert result["health"]["configured"] is True
    assert result["health"]["zoom_orphans"] == 1
    assert result["health"]["fireflies_orphans"] == 0
