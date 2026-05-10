# SPEC — Note Taker Agent

> **Статус:** draft v1, 2026-05-08
> **Owner:** Артём Соколов (CEO Office)
> **Mission:** Принимать любую meeting-запись (видео/аудио/готовый транскрипт), производить структурированную «карточку встречи»: участники, итог, решения, действия. Распространять её в Slack, Telegram, Google Doc, и через webhook во внешние системы (n8n).

## 1. Scope

**В scope:**
- Подписка на источники с записями
- Транскрибация (если её нет)
- Резка участников и канонизация имён через справочник `team_members`
- Detailed summary (полный отчёт) → Google Doc
- Short summary (1-2 KB) → Slack/TG/webhook
- To-Do block → передаётся в **Task Tracker Agent** (см. отдельный спек)
- Quality gate: галлюцинации Whisper / no-content summaries / битые записи отбрасываются ДО публикации
- Идемпотентность: повторный запуск pipeline на той же записи ничего не дублирует

**Вне scope:**
- Назначение задач конкретным людям → передаётся в Task Tracker
- Рассылка статусов / digest'ов по задачам
- Сами задачи как trackable entities (только их извлечение из транскрипта)

## 2. Sources

| Источник | Идентификатор | Транспорт | Готовность |
|---|---|---|---|
| **Zoom Cloud Recordings** | `zoom_id` (base64 UUID) | REST `/accounts/me/recordings` через S2S OAuth | ✅ работает |
| **Fireflies** | `fireflies_id` (ULID) | GraphQL API через Bearer token | ✅ работает |
| **Google Meet** (через Drive) | `gmeet_recording_id` (Drive file id) | Drive API + Meet API метаданные | ⏳ TODO |
| **Uploaded audio file** (drag-n-drop / TG voice) | `manual_upload_id` (UUID) | Telegram bot voice-note download / web upload | ⏳ TODO |
| **Voice dictation** (TG voice → live transcribe) | `dictation_id` | Telegram bot voice-note → Whisper стрим | ⏳ TODO |

Будущие: Riverside.fm, Otter.ai (если оператор начнёт ими пользоваться).

## 3. Архитектура pipeline'a

Один **универсальный pipeline** с source-specific download-step. Все остальные шаги одинаковые.

```
┌──────────────┐
│ Listener     │  (poll каждые 60s по каждому источнику)
│ (per source) │  ZoomPoller, FirefliesPoller, MeetPoller, ManualUploadHandler
└──────┬───────┘
       │ нашёл новую запись (id ∉ DB)
       v
┌──────────────────────────────────────────────────────────┐
│ Pipeline (idempotent steps, step-flag в DB)              │
│                                                           │
│ 1. download_audio          (skip if cached)               │
│    ├─ source=zoom    → audio_url из API                   │
│    ├─ source=fireflies → audio_url или transcript_url    │
│    ├─ source=gmeet   → Drive file download                │
│    └─ source=manual  → uploaded file path                 │
│                                                           │
│ 2. transcribe              (skip if transcript_text)      │
│    ├─ Whisper API call (gpt-4o-transcribe-diarize default)│
│    ├─ chunking >24MB                                      │
│    ├─ bias-prompt: team_members + counterparties          │
│    └─ fallback to source-provided transcript (Zoom VTT,   │
│       Fireflies sentences) если Whisper hallucinated      │
│                                                           │
│ 3. quality_gate            (← STOP HERE если мусор)       │
│    ├─ is_transcript_unsummarizable: <800 chars / subtitle │
│    │  markers ≥2 → mark done, не идём дальше              │
│                                                           │
│ 4. resolve_participants    (LLM)                          │
│    ├─ extract real_names из team_members + external email │
│    └─ store в row.participants                            │
│                                                           │
│ 5. detailed_summary        (LLM, gpt-5.4 default)         │
│    └─ store в row.detailed_summary (~10-30KB)             │
│                                                           │
│ 6. doc_export              (Google Drive API)             │
│    ├─ create Doc в Shared Drive folder (env)              │
│    ├─ contents = detailed_summary                         │
│    └─ store в row.google_doc_url                          │
│                                                           │
│ 7. match_calendar_title    (Google Calendar API)          │
│    ├─ ищем event в meeting_date ±30 мин окно              │
│    ├─ если найден → row.title = canonical event title     │
│    └─ push back в Fireflies UI (для Fireflies-источника)  │
│                                                           │
│ 8. match_counterparties    (LLM extract + resolve)        │
│    ├─ извлекаем mentions из транскрипта                   │
│    ├─ resolve в counterparties table                      │
│    └─ unresolved → enroll widget                          │
│                                                           │
│ 9. extract_tasks           (LLM)                          │
│    └─ список действий → передаём в Task Tracker           │
│                                                           │
│ 10. verify_tasks           (LLM, second pass)             │
│ 11. canonicalize_task_names (LLM)                         │
│ 12. consolidate_tasks      (LLM merge / split)            │
│ 13. dedupe_tasks           (fuzzy matching)               │
│                                                           │
│ 14. short_summary          (LLM, gpt-5.4 default)         │
│    ├─ first line = "DD/MM - <calendar/Zoom title>"        │
│    ├─ wrap в HTML link на google_doc_url                  │
│    └─ no-content guard: если LLM сказал "содержательная   │
│       часть отсутствует" → mark done, не публикуем        │
│                                                           │
│ 15. notify_telegram        (DM admin'ам + owner'ам)       │
│ 16. notify_slack           (channel post + thread chunks) │
│ 17. notify_webhook         (POST JSON в n8n)              │
│ 18. post_task_cards        (TG cards для каждой задачи)   │
│                                                           │
└──────────────────────────────────────────────────────────┘
```

Каждый step:
- Имеет flag в БД (`audio_downloaded`, `transcribed`, `detailed_summarised`, `tasks_extracted`, etc)
- Идемпотентен — short-circuit если flag=true
- При ошибке пишет `last_error`, не блокирует остальные

## 4. Quality gates (defensive layers)

| Gate | Когда | Что ловит | Действие |
|---|---|---|---|
| **L1: file size sanity** | После download | Audio = 0 bytes / size>cap | `last_error="audio failed"`, retry в next tick |
| **L2: Whisper hallucination** | После transcribe | Subtitle credits, low unique-word ratio, bigram loop | Fallback на VTT (Zoom) или Fireflies-provided transcript |
| **L3: thin transcript** | Перед detailed_summary | <800 chars или ≥2 subtitle markers | mark done, не идём в LLM, не публикуем |
| **L4: empty detailed_summary** | После detailed_summary LLM | LLM вернул пустую строку | retry в next tick |
| **L5: no-content short_summary** | После short_summary LLM | Содержит "содержательная часть отсутствует" / "восстановить невозможно" / "доступны только служебные пометки" | mark done, не публикуем нигде |

## 5. Outputs

| Канал | Формат | Контент | Кому |
|---|---|---|---|
| **Google Doc** | docs.google.com link | detailed_summary | shared в Drive folder + email participants |
| **Slack** | mrkdwn, threading on >3500 chars | short_summary с HTML→mrkdwn | env-channel (default: D0ASY5QF6UX = Артём DM) |
| **Telegram** | HTML, splitting on >4096 chars | short_summary | admin_user_ids env list |
| **Webhook** | JSON POST, application/json | full payload (см. ниже) | env-URL (default: n8n at thehumanoid.app.n8n.cloud) |
| **DB** | Postgres | row в `zoom_recordings`/`meeting_recordings` | для Task Tracker и аналитики |

### Webhook payload schema

```json
{
  "source": "zoom" | "fireflies" | "gmeet" | "manual" | "dictation",
  "source_id": "...",
  "title": "07/05 - Strategic Investors (Alina, Jochen, Irina)",
  "meeting_date": "2026-05-07T11:02:19+00:00",
  "duration_seconds": 3600,
  "short_summary": "<HTML с линком>",
  "detailed_summary": "## Раздел 1...",
  "google_doc_url": "https://docs.google.com/...",
  "participants": ["Alina Kolpakova", "Irina Shipilova", "ext@email.com"],
  "tasks_count": 12
}
```

### Public read-only DB view для downstream consumers

```sql
CREATE VIEW meeting_summaries_published AS
SELECT 'zoom' AS source, zoom_id AS source_id, title, meeting_date,
       duration_seconds, tasks_extracted_count, short_summary,
       detailed_summary, google_doc_url, transcript_text
FROM zoom_recordings WHERE short_summary IS NOT NULL
UNION ALL
SELECT 'fireflies', fireflies_id, title, ... FROM meeting_recordings WHERE ...;
```

GRANT SELECT to `zoom_colleague` (read-only role) — внешние BI/n8n могут читать.

## 6. Configuration (env)

```ini
# OpenAI / models
OPENAI_API_KEY=sk-proj-...
OPENAI_MODEL=gpt-4o                    # general default
FIREFLIES_SUMMARY_MODEL=gpt-5.4        # detailed_summary
FIREFLIES_SHORT_SUMMARY_MODEL=gpt-5.4  # short_summary
FIREFLIES_TASKS_MODEL=gpt-4o           # task extract / counterparty
FIREFLIES_TASKS_REASONING_EFFORT=low
FIREFLIES_WHISPER_MODEL=gpt-4o-transcribe-diarize

# Источники (включаем/выключаем независимо)
ZOOM_REALTIME_ENABLED=true
ZOOM_ACCOUNT_ID=...
ZOOM_CLIENT_ID=...
ZOOM_CLIENT_SECRET=...
ZOOM_POLL_INTERVAL_SECONDS=60
ZOOM_POLL_BATCH_SIZE=50

FIREFLIES_REALTIME_ENABLED=true
FIREFLIES_API_TOKEN=...
FIREFLIES_POLL_INTERVAL_SECONDS=60
FIREFLIES_POLL_BATCH_SIZE=50

GMEET_REALTIME_ENABLED=false           # TODO
MANUAL_UPLOAD_ENABLED=false            # TODO

# Outputs
SLACK_BOT_TOKEN=xoxb-...
SLACK_MEETING_CHANNEL_ID=D0ASY5QF6UX
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ADMIN_USER_IDS=222968032,700469400
MEETING_WEBHOOK_URL=https://thehumanoid.app.n8n.cloud/webhook/...

# Google Drive (для doc_export)
GOOGLE_SERVICE_ACCOUNT_JSON_PATH=/app/secrets/sa.json
FIREFLIES_DOCS_FOLDER_ID=1-5_A3VoQ7wydldMvAZqDfLT4YrobWNAx  # Shared Drive folder

# Calendar (для match_calendar_title — Fireflies-only)
GOOGLE_CALENDAR_CLIENT_ID=...
GOOGLE_CALENDAR_CLIENT_SECRET=...
GOOGLE_CALENDAR_ID=primary,c_*****@group.calendar.google.com  # comma-separated
CALENDAR_MATCH_ENABLED=true
CALENDAR_MATCH_WINDOW_MINUTES=30
```

## 7. Database

### Таблицы

| Таблица | Источник | Ключевые колонки |
|---|---|---|
| `zoom_recordings` | Zoom | `zoom_id`, `title`, `meeting_date`, `transcript_text`, `detailed_summary`, `short_summary`, `google_doc_url`, `participants`, step-flags |
| `meeting_recordings` | Fireflies | `fireflies_id`, симметрично Zoom |
| `gmeet_recordings` | Google Meet | `gmeet_id`, симметрично |
| `manual_upload_recordings` | Manual / dictation | `upload_id`, симметрично |

### Step-flags (общие для всех источников)

```
audio_downloaded BOOLEAN
transcribed BOOLEAN
detailed_summarised BOOLEAN
tasks_extracted BOOLEAN
short_summary_sent BOOLEAN
doc_exported BOOLEAN
last_error TEXT NULL
processed_at TIMESTAMPTZ
```

Skip-условие orphan-retry: `tasks_extracted=true AND last_error IS NULL`.

## 8. Failure modes / retries

| Сценарий | Поведение |
|---|---|
| Audio not yet ready (Zoom processing) | `last_error="no audio_url"`, retry next tick (60s) |
| Whisper API rate-limit (429) | OpenAI SDK auto-retry с exp backoff (3 attempts), затем `last_error="transcribe failed"`, retry next tick |
| Whisper hallucination | Fallback на VTT/Fireflies sentences |
| Detailed summary returns empty | `last_error="detailed summary returned empty"`, retry |
| Slack post fails (rate limit / channel not found) | log warning, **не блокирует** остальное |
| Webhook returns non-2xx | log warning, **не retry** (fire-and-forget) |
| Google Doc creation fails (quota / permission) | log warning, `google_doc_url` остаётся NULL, остальное продолжает |
| **Container reset** | All in-flight pipelines die SIGKILL'ом → DB-row остаётся mid-state → next tick подхватит как orphan, idempotent steps пропустят cached |

## 9. Observability

### Логи (structlog JSON)

Ключевые события:
```
listener_<source>_poll_loop_start
listener_<source>_poll_recording_start
<source>_step_started step=<name>
<source>_step_done step=<name> duration_ms=...
<source>_step_failed step=<name> error=...
<source>_pipeline_skipped_thin_transcript reason=...
<source>_pipeline_skipped_no_content_summary
<source>_slack_mirror_posted body_chars=... chunks_posted=...
meeting_webhook_posted source=... source_id=... status=200 ok=True
<source>_pipeline_summary <full report>
```

### Метрики (TODO: Prometheus exporter)

- `meeting_pipeline_duration_seconds{source, step}` — histogram
- `meeting_pipeline_errors_total{source, step, error_type}` — counter
- `whisper_hallucinations_detected_total{source}`
- `slack_mirror_posts_total{ok}`
- `webhook_posts_total{status}`

### Healthcheck (TODO)

HTTP `/health`:
- ✅ DB connection ok
- ✅ Last successful poll < 5 min ago
- ✅ No errored pipelines stuck >1 hour

## 10. Deployment

### Текущее (1 контейнер всё-в-одном)

```
slack-task-tg-listener (--restart unless-stopped)
  - polls Zoom, Fireflies каждые 60s
  - polls TG view (для Task Tracker, не Note Taker)
  - запускает pipelines в-process inline
```

### Целевое (раздельные контейнеры)

```
note-taker-zoom-listener
note-taker-fireflies-listener
note-taker-gmeet-listener        (когда добавим)
note-taker-manual-handler        (HTTP webhook для upload)
```

Преимущества: independent scaling, restart одного не валит другие, разные Whisper-модели per source.

## 11. Roadmap

| Quarter | Feature |
|---|---|
| Q2-2026 | GMeet integration (Drive + Meet API) |
| Q2-2026 | Manual upload (web UI или TG voice → upload) |
| Q2-2026 | Voice dictation: TG voice → live Whisper stream → instant card |
| Q3-2026 | Speaker diarization annotation в transcript_text (timestamp + speaker label) |
| Q3-2026 | Multi-language detection + per-language summarization |
| Q4-2026 | Sentiment / topic clustering across встреч |
| Q4-2026 | Видео-summary (visual context: slides, whiteboard) |

## 12. Связь с Task Tracker

Note Taker **извлекает** задачи (steps 9-13), **сохраняет** их в `tasks` table с `source_kind=zoom/fireflies/gmeet/manual`. Task Tracker **подхватывает** их через DB-trigger или polling (см. SPEC_TASK_TRACKER.md), назначает owner'ов через team_members, ставит deadlines, шлёт в TG cards.

То есть Note Taker — **источник** задач из встреч, Task Tracker — **управление** их жизненным циклом.

---

**Версии:**
- v1 (2026-05-08) — initial draft, фиксирует текущее состояние Zoom + Fireflies; gmeet/manual/dictation в TODO
