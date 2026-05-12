"""CEO Brain analytics service.

Reads from the external Humanoid CEO Brain Postgres database (the
read-only role `zoom_colleague` via pg-proxy at 34.62.139.101:5433).
Returns dicts that the Streamlit section renders as KPIs, charts,
and tables.

Connection: env var `CEO_BRAIN_DB_URL` (full postgresql:// DSN).
Empty / invalid URL → all calls return safe defaults (zeros), and the
section shows a "not configured" banner. No exceptions propagate.

Read-only by design — service exposes only SELECT queries. Filters
restrict by date range so the dashboard can show "last 7 days" /
"last 30 days" / etc.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class CEOBrainFilters:
    """Window для всех queries в CEO Brain section."""

    start_date: date
    end_date: date
    source: str | None = None  # None / "telegram" / "slack" / "zoom" / "fireflies" / "email" / "manual"

    @property
    def start_dt(self) -> datetime:
        return datetime.combine(self.start_date, datetime.min.time()).replace(tzinfo=timezone.utc)

    @property
    def end_dt(self) -> datetime:
        return datetime.combine(self.end_date, datetime.max.time()).replace(tzinfo=timezone.utc)


_DEFAULT_KPIS: dict[str, Any] = {
    "tasks_total": 0,
    "tasks_by_source": {},
    "meetings_zoom_total": 0,
    "meetings_fireflies_total": 0,
    "meetings_zoom_summarised": 0,
    "meetings_fireflies_summarised": 0,
    "top_assignees": [],
    "tasks_by_status": {},
    "tasks_by_priority": {},
    "tg_messages_total": 0,
    "tg_messages_classified_as_task": 0,
    "health": {
        "configured": False,
        "reason": "CEO_BRAIN_DB_URL not set",
    },
}


def _connect():  # noqa: ANN202 — psycopg.Connection type optional
    """Return a Postgres connection or None. Catches everything —
    section must render even with broken DB credentials."""
    dsn = os.environ.get("CEO_BRAIN_DB_URL", "").strip()
    if not dsn:
        return None
    try:
        import psycopg

        return psycopg.connect(dsn, connect_timeout=5)
    except Exception as e:  # noqa: BLE001
        log.warning("ceo_brain_connect_failed: %s", e)
        return None


def get_kpis(filters: CEOBrainFilters) -> dict[str, Any]:
    """Main KPI bundle used by the section's hero KPIs + breakdowns.

    Failures (no DSN, connection error, missing table) → returns
    `_DEFAULT_KPIS` with health.reason explaining why.
    """
    conn = _connect()
    if conn is None:
        return dict(_DEFAULT_KPIS)
    result: dict[str, Any] = {}
    start = filters.start_dt
    end = filters.end_dt
    src_filter = (filters.source or "").strip().lower()
    try:
        with conn:
            with conn.cursor() as cur:
                # Tasks total + by source
                where_src = "AND source_kind = %s" if src_filter else ""
                params: list[Any] = [start, end]
                if src_filter:
                    params.append(src_filter)
                cur.execute(
                    f"""
                    SELECT source_kind, COUNT(*)
                    FROM tasks
                    WHERE created_at BETWEEN %s AND %s
                      AND deleted_at IS NULL
                      {where_src}
                    GROUP BY source_kind
                    """,
                    params,
                )
                tasks_by_source = {row[0]: row[1] for row in cur.fetchall()}
                result["tasks_by_source"] = tasks_by_source
                result["tasks_total"] = sum(tasks_by_source.values())

                # By status
                cur.execute(
                    f"""
                    SELECT COALESCE(status, 'unknown'), COUNT(*)
                    FROM tasks
                    WHERE created_at BETWEEN %s AND %s
                      AND deleted_at IS NULL
                      {where_src}
                    GROUP BY status
                    """,
                    params,
                )
                result["tasks_by_status"] = {row[0]: row[1] for row in cur.fetchall()}

                # By priority
                cur.execute(
                    f"""
                    SELECT COALESCE(priority, 'medium'), COUNT(*)
                    FROM tasks
                    WHERE created_at BETWEEN %s AND %s
                      AND deleted_at IS NULL
                      {where_src}
                    GROUP BY priority
                    """,
                    params,
                )
                result["tasks_by_priority"] = {row[0]: row[1] for row in cur.fetchall()}

                # Top assignees
                cur.execute(
                    f"""
                    SELECT COALESCE(NULLIF(owner_display_name, ''), 'unassigned'),
                           COUNT(*) AS n
                    FROM tasks
                    WHERE created_at BETWEEN %s AND %s
                      AND deleted_at IS NULL
                      {where_src}
                    GROUP BY owner_display_name
                    ORDER BY n DESC
                    LIMIT 15
                    """,
                    params,
                )
                result["top_assignees"] = [
                    {"assignee": row[0], "tasks": row[1]} for row in cur.fetchall()
                ]

                # Meetings — Zoom
                cur.execute(
                    """
                    SELECT COUNT(*) AS total,
                           COUNT(*) FILTER (WHERE short_summary IS NOT NULL) AS summarised
                    FROM zoom_recordings
                    WHERE meeting_date BETWEEN %s AND %s
                    """,
                    [start, end],
                )
                row = cur.fetchone()
                if row:
                    result["meetings_zoom_total"] = row[0] or 0
                    result["meetings_zoom_summarised"] = row[1] or 0

                # Meetings — Fireflies
                cur.execute(
                    """
                    SELECT COUNT(*) AS total,
                           COUNT(*) FILTER (WHERE short_summary IS NOT NULL) AS summarised
                    FROM meeting_recordings
                    WHERE meeting_date BETWEEN %s AND %s
                    """,
                    [start, end],
                )
                row = cur.fetchone()
                if row:
                    result["meetings_fireflies_total"] = row[0] or 0
                    result["meetings_fireflies_summarised"] = row[1] or 0

                # TG messages processed (telegram source = ingest output)
                # `processed_telegram_messages` table holds the seen-dedup map;
                # row count = messages observed by the listener.
                try:
                    cur.execute(
                        """
                        SELECT COUNT(*) AS total,
                               COUNT(*) FILTER (WHERE task_id IS NOT NULL) AS task_extracted
                        FROM processed_telegram_messages
                        WHERE processed_at BETWEEN %s AND %s
                        """,
                        [start, end],
                    )
                    row = cur.fetchone()
                    if row:
                        result["tg_messages_total"] = row[0] or 0
                        result["tg_messages_classified_as_task"] = row[1] or 0
                except Exception as e:  # noqa: BLE001
                    log.info("ceo_brain_tg_messages_unavailable: %s", e)
                    result["tg_messages_total"] = 0
                    result["tg_messages_classified_as_task"] = 0

                # Health: latest tick + orphans + errors
                health = _health_indicators(cur)
                result["health"] = health

    except Exception as e:  # noqa: BLE001
        log.warning("ceo_brain_get_kpis_failed: %s", e)
        out = dict(_DEFAULT_KPIS)
        out["health"] = {"configured": True, "reason": f"query error: {e}"}
        return out
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    return _backfill(result)


def get_recent_meetings(filters: CEOBrainFilters, limit: int = 20) -> list[dict[str, Any]]:
    """Recent meetings table — для отображения списком."""
    conn = _connect()
    if conn is None:
        return []
    out: list[dict[str, Any]] = []
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 'zoom' AS src, zoom_id AS id, title, meeting_date,
                           duration_seconds,
                           tasks_extracted_count,
                           length(short_summary) AS short_chars,
                           google_doc_url
                    FROM zoom_recordings
                    WHERE meeting_date BETWEEN %s AND %s
                    UNION ALL
                    SELECT 'fireflies', fireflies_id, title, meeting_date,
                           duration_seconds, tasks_extracted_count,
                           length(short_summary), google_doc_url
                    FROM meeting_recordings
                    WHERE meeting_date BETWEEN %s AND %s
                    ORDER BY 4 DESC
                    LIMIT %s
                    """,
                    [filters.start_dt, filters.end_dt, filters.start_dt, filters.end_dt, limit],
                )
                for src, mid, title, mdate, dur, tcount, schars, gurl in cur.fetchall():
                    out.append({
                        "source": src,
                        "id": mid,
                        "title": (title or "")[:80],
                        "meeting_date": mdate,
                        "duration_min": (dur or 0) // 60 if dur else 0,
                        "tasks_count": tcount or 0,
                        "summary_chars": schars or 0,
                        "google_doc_url": gurl,
                        "published": bool(schars and schars > 0),
                    })
    except Exception as e:  # noqa: BLE001
        log.warning("ceo_brain_get_recent_meetings_failed: %s", e)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return out


def get_daily_tasks_series(filters: CEOBrainFilters) -> list[dict[str, Any]]:
    """Tasks-per-day timeseries for plotting (split by source)."""
    conn = _connect()
    if conn is None:
        return []
    out: list[dict[str, Any]] = []
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DATE(created_at) AS day,
                           source_kind,
                           COUNT(*) AS n
                    FROM tasks
                    WHERE created_at BETWEEN %s AND %s
                      AND deleted_at IS NULL
                    GROUP BY 1, 2
                    ORDER BY 1
                    """,
                    [filters.start_dt, filters.end_dt],
                )
                for day, src, n in cur.fetchall():
                    out.append({"day": day, "source": src, "tasks": n})
    except Exception as e:  # noqa: BLE001
        log.warning("ceo_brain_daily_series_failed: %s", e)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return out


def _health_indicators(cur) -> dict[str, Any]:  # noqa: ANN001
    """Tech-health snapshot: orphans, recent errors, last poll."""
    out: dict[str, Any] = {"configured": True, "reason": None}
    try:
        cur.execute(
            "SELECT COUNT(*) FROM zoom_recordings "
            "WHERE tasks_extracted=false OR last_error IS NOT NULL"
        )
        out["zoom_orphans"] = cur.fetchone()[0]
    except Exception:  # noqa: BLE001
        out["zoom_orphans"] = None
    try:
        cur.execute(
            "SELECT COUNT(*) FROM meeting_recordings "
            "WHERE tasks_extracted=false OR last_error IS NOT NULL"
        )
        out["fireflies_orphans"] = cur.fetchone()[0]
    except Exception:  # noqa: BLE001
        out["fireflies_orphans"] = None
    try:
        cur.execute(
            "SELECT MAX(processed_at) FROM zoom_recordings"
        )
        out["zoom_last_processed_at"] = cur.fetchone()[0]
    except Exception:  # noqa: BLE001
        out["zoom_last_processed_at"] = None
    try:
        cur.execute(
            "SELECT MAX(processed_at) FROM meeting_recordings"
        )
        out["fireflies_last_processed_at"] = cur.fetchone()[0]
    except Exception:  # noqa: BLE001
        out["fireflies_last_processed_at"] = None
    return out


def _backfill(result: dict[str, Any]) -> dict[str, Any]:
    """Заполняем missing keys значениями по умолчанию (для UI safety)."""
    out = dict(_DEFAULT_KPIS)
    out.update(result)
    if "configured" not in out["health"]:
        out["health"]["configured"] = True
    return out


def make_default_filters(days: int = 7) -> CEOBrainFilters:
    end = date.today()
    start = end - timedelta(days=days - 1)
    return CEOBrainFilters(start_date=start, end_date=end)


__all__ = [
    "CEOBrainFilters",
    "get_kpis",
    "get_recent_meetings",
    "get_daily_tasks_series",
    "make_default_filters",
]
