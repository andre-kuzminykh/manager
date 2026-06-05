# Передача дел — Humanoid CEO Brain Manager

> **Кому:** инженеру, продолжающему работу с системой.
> **Дата:** 2026-06-05.
> **Ветка:** `claude/slack-bot-task-extraction-9eGSC` (запушена в `andre-kuzminykh/manager`).
> Этот документ — точка входа. Всё необходимое — внутри архива.

---

## 1. Что это

**Humanoid CEO Brain Manager** — монолитный Python-сервис (FastAPI/Bolt + Postgres),
который слушает рабочие каналы оператора, превращает разговоры и встречи в
структурированные задачи + саммари + брифы, ведёт их жизненный цикл и зеркалит
всё в Google (Sheets / Tasks / Docs / Calendar).

Пользователь — CEO компании Humanoid.

### Главные фичи

| Фича | Что делает | Спека |
|---|---|---|
| **Note Taker** | Zoom + Fireflies записи → транскрипт → детальное саммари + задачи + Google Doc + Slack/TG | `SPEC_NOTE_TAKER_v0.1.md` |
| **Meeting Webhook** | Полное саммари встречи в n8n-вебхук → внешние системы | `SPEC_MEETING_WEBHOOK_v0.1.md` |
| **Meeting Agenda** | За 10 мин до regular-встречи Calendar — авто-повестка в Slack DM | `SPEC_MEETING_AGENDA_v0.1.md` |
| **Counterparty Briefs** | Calendar → новый контрагент → deep-research (компания + бенефициары) → Google Doc + Slack | `SPEC_COUNTERPARTY_BRIEFS_v0.1.md` |
| **Entity Consistency** | Единая каноническая форма имён сущностей по всей встрече (detailed/tasks/short/Doc) | `SPEC_ENTITY_CONSISTENCY_v0.1.md` |
| **Entity Critic / FR-каталог** | Резолв сущностей через локальную реплику CRM-каталога (~22 листов, daily sync) + shard + critic | `SPEC_ENTITY_CRITIC_v0.1.md` |
| **Native Transcript** | Нативные транскрипты сервисов (Zoom VTT / Fireflies sentences) как primary, Whisper — fallback | `SPEC_NATIVE_TRANSCRIPT_v0.1.md` |
| **Task Vector** | Embedding-индекс задач, NL-поиск/Q&A/апдейт через MCP-сервер | `docs/SPEC_TASK_VECTOR_v0.1.md` |
| **CEO Brain Bot** | Slack-агент с памятью (архив каналов) + ответы через Claude + MCP (Slack/Gmail/Calendar/Drive/Pitchbook/Hubspot/...) | `SPEC_CEO_BRAIN_BOT_v0.1.md` |
| **Status Tracker** | Встречи → авто-апдейт статусов задач в Google Sheet с логом и откатом (поверх движка Task Vector) | `SPEC_STATUS_TRACKER_v0.2.md` |
| **Sheet Sync** | Двунаправленный мост Sheet ↔ БД (uuid-идентичность, append-only log) | `SPEC_SHEET_SYNC_v0.1.md` |

`PRD.md` в корне — мастер-индекс с актуальным статусом каждой фичи.
`AUDIT.md` — детальная FR-by-FR карта соответствия спек и кода.

---

## 2. Архитектура (коротко)

### Контейнеры в проде

| Контейнер | Образ | Назначение |
|---|---|---|
| `manager-db-1` | `postgres:16-alpine` | основная БД `slack_tasks` |
| `manager-bot-1` | `manager-bot:latest` | Slack-бот (Socket Mode) + TG-handlers + демоны Agenda и Counterparty Briefs + CEO Brain Bot |
| `manager-zoom-ff-1` | `manager-bot:v2shadow` | always-on Zoom/Fireflies polling-runner (две нитки в одном процессе) |

В проде запуск через `docker run` с env инлайн (НЕ `docker-compose up` —
compose-файл в репе для локального dev).

### Точки входа в коде

| Компонент | Файл |
|---|---|
| Slack-bot entry | `app/main.py` → `app.slack_bot.app.build_app` |
| Zoom/FF polling-runner | `ops/zoom_fireflies_runner.py` |
| Ручной republish одной встречи | `ops/republish_meeting.py` |
| Zoom pipeline | `app/zoom/pipeline.py` (`ZoomPipeline.process_one`) |
| Fireflies pipeline | `app/fireflies/pipeline.py` (`FirefliesPipeline.process_one`) |
| Entity resolver FR (CRM-каталог) | `app/services/entity_resolver_fr.py`, `app/services/fr_catalog_replica.py` |
| Канонизация имён | `app/services/counterparty_match.py:canonicalize_text` |
| Sheet sync (активный) | `app/sync/sheets.py` |
| Календарь / attendees | `app/services/calendar_attendees.py` |
| Slack mirror | `app/services/slack_mirror.py` |
| n8n meeting webhook | `app/services/meeting_webhook.py` |
| Advisory-локи (per-meeting) | `app/services/pg_lock.py` |
| Конфиг (Pydantic Settings) | `app/config.py` |
| Модели | `app/models/` |
| Миграции | `alembic/versions/0001..0042_*.py` |

### Pipeline встречи (одной строкой)

```
list → download_audio → transcribe → detailed_summary → match_counterparties
     → canonicalize_task_names → consolidate_tasks → extract_tasks → verify_tasks
     → doc_export → short_summary → post_task_cards → slack_mirror → n8n_webhook
```

Каждый шаг — отдельный флаг на строке `zoom_recordings` / `meeting_recordings`,
шаги идемпотентны, прерванная встреча возобновляется с того места, где упала.
Подробно — `docs/PIPELINE_FLOW.md`.

---

## 3. Развёртывание

### 3.1 Деплой обновлений

```bash
# 1. Залить код на хост (если он git-checkout)
cd ~/manager-zff && git pull

# 2. Если поменялся импорт / появился новый модуль — пересобрать образ:
docker build -t manager-bot:v2shadow .

# 3. Перезапустить:
docker restart manager-zoom-ff-1
docker restart manager-bot-1

# 4. Логи:
docker logs --since 10m -f manager-zoom-ff-1
# искать строки: zoom_ff_runner_starting, ff_runner_tick, zoom_runner_tick
```

**Важно:** in-prod раннер монтируется с хоста, но Python-процесс
**не перечитывает файл сам**. После любой правки кода — `docker restart`
обязателен, иначе процесс продолжит крутить старый код в памяти.

### 3.2 Миграции

```bash
docker exec manager-bot-1 alembic upgrade head
```

Последняя миграция в репе — `0042_fr_catalog_snapshots`.

### 3.3 Per-meeting advisory-локи

Раннер и ручные ops берут `pg_try_advisory_xact_lock` per meeting через
`app/services/pg_lock.py:try_meeting_lock`, чтобы одна встреча не
обрабатывалась двумя процессами одновременно. Лок транзакционный —
освобождается при коммите/роллбэке `session_scope`.

---

## 4. Где смотреть оперативку

```bash
# Логи раннера за сутки:
docker logs --since 24h manager-zoom-ff-1 2>&1 | grep -E "zoom_runner_tick|ff_runner_tick"

# Сколько встреч обработано:
docker exec -i manager-db-1 psql -U postgres -d slack_tasks <<'SQL'
SELECT date_trunc('hour', processed_at) AS h, COUNT(*)
FROM zoom_recordings WHERE processed_at > now() - interval '24 hours'
GROUP BY 1 ORDER BY 1;
SQL

# Свежие задачи:
docker exec -i manager-db-1 psql -U postgres -d slack_tasks -c \
  "SELECT id, title, owner, status, due_at FROM tasks ORDER BY created_at DESC LIMIT 20;"
```

---

## 5. Регламент работы

1. **Спека → тесты → код.** Перед изменением логики:
   найди (или напиши) FR в `SPEC_*.md` → открой/добавь тест в
   `tests/requirements/` → запусти и убедись что красный → меняй код →
   тест становится зелёным.
2. **Один коммит = один FR-CR-05-NNN.** Номер инкрементируется
   (текущий максимум — 258). В сообщении коммита и в комментариях.
3. **Комментарии в коде — только "WHY".** Что код делает — видно по коду;
   почему именно так — комментарий обязателен.
4. **Раннер не перезагружает код сам.** Любая правка → `docker restart`.
5. **DB-локи.** Любая фоновая обработка одной «сущности» (recording, task,
   meeting) — оборачивай в `try_meeting_lock` или аналогичный
   `pg_try_advisory_xact_lock`. Шаблон — `app/services/pg_lock.py`.
6. **Никаких force-push, amend, `--no-verify`.**

---

## 6. Документация в репе (читать в этом порядке)

1. **`PRD.md`** — мастер-индекс фич и их статус.
2. **`AGENTS.md`** — топология контейнеров, env-инжект, как ходить в БД.
3. **`docs/PIPELINE_FLOW.md`** — пошагово, как одна встреча проходит pipeline.
4. **`docs/TECHNICAL_ARCHITECTURE.md`** — общая архитектура + ID точек входа.
5. **`AUDIT.md`** — FR-by-FR карта соответствия спек и кода (на дату аудита).
6. **`SPEC_*.md`** (в корне) — спеки фичей. Имена FR в коде — `FR-CR-05-*`,
   а в спеках свои (`FR-NT-*`, `FR-EC-*`, `FR-TV-*` и т.п.); сверка — в `AUDIT.md`.
7. **`docs/FR_TEST_MATRIX.md`** — какие тесты покрывают какой FR.
8. **`DEPLOY.md`** — запуск и деплой подробно.
9. **`docs/archive/`** — исторические редакции спек и старые монолиты.

---

## 7. Среда разработки

```bash
# Установка зависимостей:
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Локальная БД:
docker-compose up -d db
alembic upgrade head

# Тесты:
python -m pytest tests/ -q

# Линтер / типы:
ruff check .
mypy app/
```

### Конфигурация

Все настройки — через env, читаются в `app/config.py` (Pydantic Settings).
`.env.example` в корне — шаблон с описанием ключевых переменных. Для прода
env прокидывается инлайн в `docker run`.

---

## 8. Ветка передачи

- **Имя:** `claude/slack-bot-task-extraction-9eGSC`
- **HEAD:** см. шапку файла
- **Все изменения запушены** в `andre-kuzminykh/manager`
- **Pull request — на твоё усмотрение** (можно мержить в `main` или
  работать дальше с этой ветки)

История последних работ — `git log --oneline -20` в репе. Каждый коммит
содержит FR-CR-05-NNN-ID и краткое описание изменений.

---

Если есть вопросы по конкретной фиче — начинай с `PRD.md`, оттуда есть
ссылки на спеку, оттуда на код (через FR-CR-05-NNN номер). `AGENTS.md`
объясняет, где что лежит в репе.

Удачи.
