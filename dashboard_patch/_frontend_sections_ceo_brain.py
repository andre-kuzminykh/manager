"""CEO Brain — задачи / встречи / health.

Section reads from the external Humanoid CEO Brain Postgres DB
through `backend.services.ceo_brain` (no SQLite). Connection
configured via `CEO_BRAIN_DB_URL` env var. If not set or DB
unreachable — section renders an info banner and stays inert.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

from backend.services.ceo_brain import (
    CEOBrainFilters,
    get_daily_tasks_series,
    get_kpis,
    get_recent_meetings,
    make_default_filters,
)
from frontend.components import fmt_int, hero, kpi_row, section


_SOURCE_COLORS = {
    "telegram": "#229ED9",
    "slack": "#611f69",
    "zoom": "#2D8CFF",
    "fireflies": "#F7B500",
    "email": "#34a853",
    "manual": "#888888",
    "google_tasks": "#4285F4",
    "recurring": "#cc88aa",
}


def _filters_bar() -> CEOBrainFilters:
    """Top filters: date range + source select."""
    col1, col2, col3 = st.columns([2, 2, 2])
    with col1:
        period_label = st.selectbox(
            "Period",
            ["Last 7 days", "Last 14 days", "Last 30 days", "Last 90 days", "Custom"],
            index=0,
        )
    today = date.today()
    period_map = {
        "Last 7 days": 7, "Last 14 days": 14,
        "Last 30 days": 30, "Last 90 days": 90,
    }
    if period_label == "Custom":
        with col2:
            start = st.date_input("From", value=today - timedelta(days=7))
        with col3:
            end = st.date_input("To", value=today)
    else:
        days = period_map[period_label]
        start = today - timedelta(days=days - 1)
        end = today
        col2.metric("From", start.strftime("%Y-%m-%d"))
        col3.metric("To", end.strftime("%Y-%m-%d"))

    src_col = st.columns([3, 1])[0]
    with src_col:
        source = st.selectbox(
            "Source filter (tasks)",
            ["All", "telegram", "slack", "zoom", "fireflies", "email", "manual", "google_tasks", "recurring"],
            index=0,
        )
    return CEOBrainFilters(
        start_date=start,
        end_date=end,
        source=None if source == "All" else source,
    )


hero(
    "CEO Brain",
    '<span class="accent">Tasks</span> & Meetings analytics',
    "Сколько сообщений приходило / задач извлечено / встреч обработано — в разрезе источников и людей.",
)

filters = _filters_bar()
kpis = get_kpis(filters)

# --- Configuration / health banner -------------------------------
health = kpis.get("health", {})
if not health.get("configured"):
    st.warning(
        f"CEO Brain DB не настроена: {health.get('reason') or 'CEO_BRAIN_DB_URL не задан'}. "
        "Передайте `CEO_BRAIN_DB_URL=postgresql://zoom_colleague:<pw>@<host>:5433/slack_tasks` "
        "в env-vars, чтобы видеть данные.",
        icon="⚠️",
    )
    st.stop()

if health.get("reason"):
    st.error(f"CEO Brain DB error: {health['reason']}", icon="❌")

# --- KPIs ----------------------------------------------------------
kpi_row(
    [
        {
            "label": "Tasks extracted",
            "value": fmt_int(kpis["tasks_total"]),
            "note": "за выбранный период",
        },
        {
            "label": "TG messages seen",
            "value": fmt_int(kpis["tg_messages_total"]),
            "note": f"{fmt_int(kpis['tg_messages_classified_as_task'])} классифицировано как задача",
        },
        {
            "label": "Zoom meetings",
            "value": fmt_int(kpis["meetings_zoom_total"]),
            "note": f"{fmt_int(kpis['meetings_zoom_summarised'])} с summary",
        },
        {
            "label": "Fireflies meetings",
            "value": fmt_int(kpis["meetings_fireflies_total"]),
            "note": f"{fmt_int(kpis['meetings_fireflies_summarised'])} с summary",
        },
    ],
    cols=4,
)

# --- Per-source breakdown -----------------------------------------
section("Задачи по источникам")

by_source = kpis.get("tasks_by_source") or {}
if by_source:
    df_src = pd.DataFrame(
        [{"source": k, "tasks": v} for k, v in by_source.items()]
    ).sort_values("tasks", ascending=False)
    fig = px.bar(
        df_src,
        x="source",
        y="tasks",
        text="tasks",
        color="source",
        color_discrete_map=_SOURCE_COLORS,
    )
    fig.update_traces(textposition="outside")
    fig.update_layout(
        showlegend=False, margin=dict(l=10, r=10, t=10, b=10), height=320,
    )
    st.plotly_chart(fig, use_container_width=True)
else:
    st.info("Нет задач за выбранный период.")

# --- Top assignees -------------------------------------------------
section("Топ assignees")

top = kpis.get("top_assignees") or []
if top:
    df_a = pd.DataFrame(top).head(15)
    fig = px.bar(
        df_a,
        x="tasks",
        y="assignee",
        orientation="h",
        text="tasks",
    )
    fig.update_layout(
        yaxis=dict(autorange="reversed"),
        margin=dict(l=10, r=10, t=10, b=10), height=420,
    )
    fig.update_traces(textposition="outside")
    st.plotly_chart(fig, use_container_width=True)
else:
    st.info("Нет данных по assignees.")

# --- Daily timeseries ---------------------------------------------
section("Динамика задач по дням")

series = get_daily_tasks_series(filters)
if series:
    df_s = pd.DataFrame(series)
    df_s["day"] = pd.to_datetime(df_s["day"])
    fig = px.line(
        df_s,
        x="day", y="tasks", color="source", markers=True,
        color_discrete_map=_SOURCE_COLORS,
    )
    fig.update_layout(margin=dict(l=10, r=10, t=10, b=10), height=320)
    st.plotly_chart(fig, use_container_width=True)
else:
    st.info("Нет данных за выбранный период.")

# --- Status / priority -------------------------------------------
col1, col2 = st.columns(2)
with col1:
    section("Tasks by status")
    by_status = kpis.get("tasks_by_status") or {}
    if by_status:
        df_status = pd.DataFrame(
            [{"status": k, "tasks": v} for k, v in by_status.items()]
        ).sort_values("tasks", ascending=False)
        fig = px.pie(df_status, names="status", values="tasks")
        fig.update_layout(margin=dict(l=10, r=10, t=10, b=10), height=300)
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Нет данных.")

with col2:
    section("Tasks by priority")
    by_prio = kpis.get("tasks_by_priority") or {}
    if by_prio:
        df_prio = pd.DataFrame(
            [{"priority": k, "tasks": v} for k, v in by_prio.items()]
        ).sort_values("tasks", ascending=False)
        fig = px.pie(df_prio, names="priority", values="tasks")
        fig.update_layout(margin=dict(l=10, r=10, t=10, b=10), height=300)
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Нет данных.")

# --- Recent meetings list -----------------------------------------
section("Свежие встречи")

meetings = get_recent_meetings(filters, limit=30)
if meetings:
    df_m = pd.DataFrame(meetings)
    df_m["meeting_date"] = pd.to_datetime(df_m["meeting_date"])
    df_m = df_m.sort_values("meeting_date", ascending=False)
    df_m_display = df_m.assign(
        Published=df_m["published"].apply(lambda x: "✅" if x else "—"),
        Doc=df_m["google_doc_url"].apply(
            lambda x: f"[link]({x})" if x else "—"
        ),
    )[["source", "meeting_date", "title", "duration_min",
       "tasks_count", "summary_chars", "Published", "Doc"]]
    df_m_display.columns = ["Source", "Date", "Title", "Min", "Tasks", "Summary chars", "Published", "Doc"]
    st.dataframe(df_m_display, hide_index=True, use_container_width=True)
else:
    st.info("Нет встреч за выбранный период.")

# --- Tech health --------------------------------------------------
section("Tech health")

zoom_orphans = health.get("zoom_orphans")
ff_orphans = health.get("fireflies_orphans")
zoom_last = health.get("zoom_last_processed_at")
ff_last = health.get("fireflies_last_processed_at")


def _fmt_ago(dt) -> str:  # noqa: ANN001
    if not dt:
        return "—"
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except Exception:  # noqa: BLE001
            return str(dt)
    delta = datetime.now(tz=dt.tzinfo or None) - dt
    mins = delta.total_seconds() // 60
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{int(mins)} min ago"
    hrs = mins // 60
    if hrs < 24:
        return f"{int(hrs)}h ago"
    days = hrs // 24
    return f"{int(days)}d ago"


kpi_row(
    [
        {"label": "Zoom orphans", "value": fmt_int(zoom_orphans or 0), "note": "tasks_extracted=false OR last_error"},
        {"label": "Fireflies orphans", "value": fmt_int(ff_orphans or 0), "note": "stuck recordings"},
        {"label": "Zoom last processed", "value": _fmt_ago(zoom_last), "note": "newest meeting"},
        {"label": "Fireflies last processed", "value": _fmt_ago(ff_last), "note": "newest meeting"},
    ],
    cols=4,
)

st.caption(
    "Note: CEO Brain DB read-only через role `zoom_colleague`. "
    "Filters применяются к создан-датам tasks и meeting_date встреч."
)
