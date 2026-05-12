# SPEC — CEO Brain section

> **Версия:** v0.1, 2026-05-12
> **Scope:** новая страница `CEO Brain` в существующем dashboard
> **Источник данных:** внешний Postgres БД проекта Humanoid CEO Brain (read-only role `zoom_colleague` через pg-proxy)

---

## 1. Mission

Дать CEO single-pane view технического health'a + операционных метрик AI-agent'ов (Note Taker, Task Extractor, Task Tracker):
- Сколько сообщений приходило / сколько превратилось в задачи
- Распределение задач по людям (bar chart) и по источникам
- Сколько встреч обработано (Zoom + Fireflies, отдельно)
- Сколько summary выпущено
- Tech health: orphans, errors, last poll timestamps

---

## 2. Сценарии использования

| Кто | Когда | Что хочет |
|---|---|---|
| CEO | Утром / в течение дня | Понять «работает ли всё» + куда уходят задачи |
| Виктор (BI) | Раз в неделю | Снять stats, экспортировать csv (через копию SQL) |
| Operator | После incident'a | Проверить orphans + last poll'ы |

---

## 3. Архитектура

```mermaid
flowchart LR
    User[CEO] --> Browser
    Browser -->|HTTPS| Streamlit[Streamlit Dashboard]
    Streamlit -->|read-only psycopg| PG[(CEO Brain Postgres)]

    subgraph "CEO Brain DB"
        Tasks[(tasks)]
        Zoom[(zoom_recordings)]
        FF[(meeting_recordings)]
        Processed[(processed_telegram_messages)]
    end

    PG --> Tasks
    PG --> Zoom
    PG --> FF
    PG --> Processed
```

### Components

| Слой | Файл | Назначение |
|---|---|---|
| Service | `backend/services/ceo_brain.py` | SQL queries; default-safe (no DSN → defaults) |
| UI | `frontend/sections/ceo_brain.py` | Streamlit page: filters, KPIs, charts, table |
| Nav | `frontend/main.py` | Adds CEO Brain entry |
| Tests | `tests/test_ceo_brain.py` | Unit + mock-DB integration |
| Config | env `CEO_BRAIN_DB_URL` | full postgresql:// DSN |

---

## 4. Features

| ID | Фича | Описание |
|---|---|---|
| F-CB-01 | KPI strip | tasks_total / tg_messages / zoom_meetings / fireflies_meetings |
| F-CB-02 | Tasks by source | bar chart (telegram / slack / zoom / fireflies / email / manual / recurring) |
| F-CB-03 | Top assignees | horizontal bar chart top-15 |
| F-CB-04 | Daily timeseries | line chart per source |
| F-CB-05 | Tasks by status | pie chart (todo/in_progress/blocked/done/cancelled) |
| F-CB-06 | Tasks by priority | pie chart (low/medium/high/urgent) |
| F-CB-07 | Recent meetings table | last 30, with Google Doc link |
| F-CB-08 | Tech health | orphans count + last processed timestamps |
| F-CB-09 | Filters | period (7/14/30/90/custom) + source filter |

---

## 5. Data sources

### Tables (read-only)

| Table | Колонки используемые |
|---|---|
| `tasks` | `id, source_kind, owner_display_name, priority, status, created_at, deleted_at` |
| `zoom_recordings` | `zoom_id, title, meeting_date, duration_seconds, short_summary, google_doc_url, tasks_extracted_count, tasks_extracted, last_error, processed_at` |
| `meeting_recordings` | то же что zoom, но `fireflies_id` |
| `processed_telegram_messages` | `chat_id, message_id, processed_at, task_id` |

### Queries (sanitized)

См. `backend/services/ceo_brain.py`. Все SELECT, без UPDATE/DELETE.

---

## 6. Functional Requirements

| ID | Требование | Test |
|---|---|---|
| FR-CB-1.1 | Service возвращает default'ы при отсутствующем `CEO_BRAIN_DB_URL` | `test_get_kpis_without_db_returns_defaults` |
| FR-CB-1.2 | Service возвращает default'ы при DB connection failure | `test_connect_failure_returns_defaults` |
| FR-CB-1.3 | KPIs включают `tasks_total, tasks_by_source, meetings_zoom, meetings_fireflies, tg_messages, top_assignees, tasks_by_status, tasks_by_priority, health` | `test_get_kpis_with_mocked_db` |
| FR-CB-1.4 | `make_default_filters(days=N)` → правильный date range | `test_make_default_filters_*` |
| FR-CB-2.1 | Section показывает warning banner если DB не настроена | (UI smoke) |
| FR-CB-2.2 | Filters: period (5 опций) + source (8 опций) | (UI smoke) |
| FR-CB-2.3 | KPI strip — 4 cards | (UI smoke) |
| FR-CB-2.4 | Tasks-by-source — bar chart | (UI smoke) |
| FR-CB-2.5 | Top assignees — horizontal bar | (UI smoke) |
| FR-CB-2.6 | Daily timeseries — line chart | (UI smoke) |
| FR-CB-2.7 | Recent meetings — DataFrame с Doc link | (UI smoke) |
| FR-CB-2.8 | Tech health — 4 KPIs (orphans + last processed) | (UI smoke) |

---

## 7. Non-Functional Requirements

| ID | Требование | Цель |
|---|---|---|
| NFR-CB-P.1 | Section loads ≤ 3s на N=10000 tasks | ≤ 3s |
| NFR-CB-R.1 | DB unavailability НЕ breaks rest of dashboard | enforced |
| NFR-CB-S.1 | DB role `zoom_colleague` только SELECT | enforced |
| NFR-CB-S.2 | DSN в env, не commit'ится в репо | enforced |
| NFR-CB-O.1 | Service errors logged via stdlib logging WARNING | enforced |

---

## 8. Deployment

### Env vars

```bash
# Required для активации секции
CEO_BRAIN_DB_URL=postgresql://zoom_colleague:Zm9JxLg2nQpRtVc4@34.62.139.101:5433/slack_tasks?sslmode=disable
```

### Verification

После deploy:
1. Открой dashboard → CEO Brain section в sidebar
2. Жди ≤ 3 секунды
3. KPIs покажут реальные числа (если DB живая)
4. Если "DB не настроена" — проверь env

### Rollback

Если что-то ломается:
1. Удалить `CEO_BRAIN_DB_URL` из env → section покажет warning, rest dashboard работает
2. Или комментировать строки в `main.py` где добавлен `ceo_brain` page

---

## 9. Tests

### Test pyramid

| Type | Count | Что |
|---|---|---|
| Unit | 5 | default-safe path, filters factory |
| Integration (mocked) | 1 | full KPI pipeline mock SQL |
| UI smoke | manual | Streamlit run + visual check |

### Test files

- `tests/test_ceo_brain.py` — 7 тестов:
  - `test_get_kpis_without_db_returns_defaults`
  - `test_get_recent_meetings_without_db_returns_empty`
  - `test_get_daily_tasks_series_without_db_returns_empty`
  - `test_make_default_filters_default_7_days`
  - `test_make_default_filters_custom_days`
  - `test_filters_start_end_dt_tzaware`
  - `test_filters_source_normalized`
  - `test_connect_failure_returns_defaults`
  - `test_get_kpis_with_mocked_db`

### Запуск

```bash
pytest tests/test_ceo_brain.py -v
```

---

## 10. Open questions

| ? | Why important | Кто отвечает |
|---|---|---|
| Cache layer (`@st.cache_data`) — нужен ли? | Performance на 100k tasks | CEO |
| Date filter timezone — UTC vs local | UX | CEO |
| Включать ли cost per task / cost per meeting? | Метрика для оптимизации LLM | CEO |
| Export CSV button — реализовывать сейчас? | Виктор просит | Виктор |

---

**Версия:** v0.1, 2026-05-12
**Maintainer:** Артём Соколов / Андрей Кузьминых
