# AGENTS.md — ориентация по репе для Claude/Codex/прочих агентов

> Прочитай ЭТО первым в новой сессии. Дальше ныряй в код только адресно.
> Последнее ревью: 2026-06-03.

---

## 0. TL;DR — что это вообще

Slack-бот + автономные daemon'ы, который:
- Слушает Slack/Telegram, вытаскивает задачи, синкает в Google Sheets/Tasks.
- Слушает Zoom Cloud Recordings и Fireflies, делает саммари + извлекает задачи.
- За полчаса до митинга кидает агенду в Slack (повестка + recap прошлой встречи + открытые задачи).
- Для встреч с внешними людьми — пишет counterparty brief в Google Doc.
- Отдельный «CEO Brain» бот в другом workspace архивирует Slack и отвечает по @mention через Claude.

Python 3.x, SQLAlchemy 2.0, Postgres 16, Pydantic settings, бот на Bolt Socket Mode. Деплой — docker compose.

---

## 1. Где код

| Путь | Что |
|---|---|
| `app/main.py` | Единый entrypoint. `run()` поднимает Slack-бот + daemon-треды агенды, брифов, CEO Brain. |
| `app/config.py` | Pydantic `Settings`. ВСЕ env-переменные тут — открой и `grep alias=` чтобы найти имена. |
| `app/db.py` | Три engine'а: основной + опциональные `CATALOG_DATABASE_URL` / `TASK_VECTOR_DATABASE_URL` (pgvector). |
| `app/models/` | 24 SQLAlchemy модели. |
| `app/agenda/` | Pre-meeting агенда (см. §4). |
| `app/fireflies/` | Fireflies ingest → транскрипт → summary + tasks. |
| `app/zoom/` | Zoom Cloud Recordings ingest → Whisper → summary + tasks. |
| `app/slack_bot/` | Bolt event handlers, draft-карточки, кнопки confirm/edit/cancel. |
| `app/slack_ingest/` | (opt-in) исторический ingest каналов. |
| `app/telegram_bot/`, `app/telegram_ingest/` | TG: входящие + ingest групп. |
| `app/ceo_brain/` | Отдельный Slack-app: архив + Claude-ответчик. **Своя БД и свой workspace.** |
| `app/intent/` | Классификатор интента: rules + Anthropic tool-use. |
| `app/orchestrator/` | Confidence policy → draft card / soft prompt → confirm → DB + sync. |
| `app/services/` | ~40 файлов с бизнес-сервисами (entity catalog, counterparty match v1/v2, bilingual restorer, planning, digests). |
| `app/sync/` | Google Sheets ↔ tasks, Google Tasks, Fernet-зашифрованные OAuth-токены. |
| `app/persistence/` | Низкоуровневое создание задач/митингов с context_snapshot. |
| `app/counterparty_briefs/` | Daemon: сканит календарь → внешние орги → Google Docs brief. |
| `alembic/versions/` | 38+ миграций. |
| `ops/*.py` | Дебаг-, миграционные- и QA-скрипты. ~100 штук. См. §7. |
| `tests/`, `tests/requirements/` | pytest. Запуск: `pytest tests` или `docker compose exec -T bot pytest`. `tests/requirements/` — FR-CR-05-XXX integration-тесты по спекам. |
| `pyproject.toml` | Зависимости + pytest конфиг. |
| `alembic.ini` | Конфиг миграций. |
| `docs/DB_SCHEMA.md` | Полный ERD по 7 доменам. |
| `SPEC_*.md` | Спеки по фичам. См. §6. |

---

## 2. Docker / прод-окружение

Прод крутится в docker compose на этой машине. Ключевые контейнеры:

| Container | Image | Роль |
|---|---|---|
| `manager-db-1` | `postgres:16-alpine` | **Основная БД.** `slack_tasks` / user `postgres` / пароль в `.env`. Порт наружу не торчит — заходи через `docker exec`. |
| `manager-bot-1` | `manager-bot` | Прод-бот: Slack listener + agenda runner + briefs + sync. |
| `manager-zoom-ff-1` | `manager-bot:v2shadow` | **Актуальный Zoom/Fireflies-пайплайн.** Команда `python -m ops.zoom_fireflies_runner`. Та же БД. Именно он обрабатывает свежие записи и постит саммари. Env прокинут инлайн при `docker run` (НЕ compose — лейблы compose пустые); кастомный runner примонтирован из `/home/andre/manager-zff/zoom_fireflies_runner.py`. |

⚠️ **На хосте ДВА деплоя этого приложения, env между ними НЕ синхронизирован:**
- `manager-*` (`manager-bot`, `manager-zoom-ff-1`) — **актуальный прод**, образ `manager-bot[:v2shadow]`, запуск через `docker run` (env инлайн).
- `slack-task-*` (`slack-task-tg-listener`, `slack-task-slack-ingest`, ...) — **старый деплой**, образ `slack-task-bot:latest`. Часть env-переменных живёт ТОЛЬКО здесь.

Грабли (2026-06-03): `MEETING_WEBHOOK_URL` (n8n-вебхук саммари коллеге) был выставлен только на старых `slack-task-*`, но они не гоняют Zoom/FF-пайплайн → вебхук молчал. Новый `manager-zoom-ff-1`, который реально обрабатывает митинги, переменную не получил. **Вывод: при правке env проверяй ОБА деплоя; переменная в репе (`.env.example`) ≠ переменная в рантайме контейнера.** Реальный env смотри: `docker inspect <c> --format '{{range .Config.Env}}{{println .}}{{end}}'` или `docker exec <c> python -c "from app.config import get_settings; print(get_settings().<field>)"`.

Оба бота смотрят в **одну и ту же** Postgres (`DATABASE_URL` идентичен — проверил). Никаких отдельных схем — таблицы общие.

**Два разных Slack-app токена** возможны: основной бот + опциональный agenda/brief bot с другими scope'ами (FR-CR-05-167). Это позволяет agenda_runner постить в каналы, которых не видит основной бот. Проверь `AGENDA_SLACK_*` и `CEO_BRAIN_SLACK_*` env'ы.

**Health-check:** в Dockerfile `CMD ["python", "-m", "app.main"]` — без HTTP-эндпойнта. Для оркестраторов с `/health` есть `ops/entrypoint_with_health.py`.

### Подключение к БД — единственный рабочий способ

`DATABASE_URL` на хосте обычно НЕ выставлен и в `~/manager/.env` его нет. Postgres слушает только внутри docker network. Поэтому:

```bash
docker exec -i manager-db-1 psql -U postgres -d slack_tasks <<'SQL'
-- твой запрос
SQL
```

Не пытайся ставить psql-клиент и коннектиться извне через сокет — он не работает.

### Логи

```bash
docker logs --since 72h manager-bot-1 2>&1 | grep -iE 'agenda_posted|agenda_skipped|...'
docker logs --since 24h manager-zoom-ff-1 2>&1 | grep zoom_pipeline_summary
```

Лог-формат — structlog JSON-ish (key=value). Имена ивентов: `agenda_posted`, `agenda_skipped_empty_body`, `agenda_prior_skipped_thin`, `agenda_tick_summary`, `zoom_pipeline_summary`, `fireflies_pipeline_summary`.

---

## 3. БД — главные таблицы

Полная схема: `docs/DB_SCHEMA.md`. Здесь — что чаще всего открываешь при дебаге.

| Таблица | Что |
|---|---|
| `tasks` | Источник правды по задачам. `source_kind ∈ {slack, telegram, fireflies, zoom}`, `source_conversation_id`, `status`, `owner_*`, `due_date`, `extra` (jsonb) — там `direction`. Soft-delete через `deleted_at`. |
| `meetings` | Митинги (созданные через intent), не путать с записями. |
| `zoom_recordings` | **Zoom-записи.** Ключ `zoom_id` (PK, base64 типа `dA2fUMqxQv6X...==`). Колонки: `title`, `meeting_date`, `transcript_text`, `short_summary`, `detailed_summary`, `short_summary_sent`, `created_at`, `updated_at`. ⚠️ Пустые/провальные записи (Whisper упал) могут чиститься фоновым cleanup'ом — строка появляется и исчезает. |
| `meeting_recordings` | **Fireflies-записи (v2).** Ключ `fireflies_id` (uniq), НЕ `zoom_id`. Колонки `id` (serial PK), `transcript_text`, `short_summary`, `detailed_summary`, флаги стадий (`audio_downloaded`/`transcribed`/`short_summary_sent`/...), `attempts`, `last_error`, `calendar_attendees`. **Не путать с `zoom_recordings`** — это РАЗНЫЕ источники, а не legacy/v2 одной записи. Если митинг ищешь — проверь обе по `title ILIKE`. |
| `meeting_agendas` | Idempotency для постов агенды: `calendar_event_id` (uniq), `slack_ts`, `posted_at`, `prior_meeting_zoom_ids` (json). |
| `intent_inferences`, `action_drafts` | Трейс работы intent-классификатора. |
| `context_snapshots` | Сообщение + 10 предыдущих + thread — audit-trail для задачи/митинга. |
| `team_members`, `employees`, `counterparties` | Три независимых справочника. |
| `entity_catalog`, `entity_embeddings` | pgvector dedup (в отдельной БД, если `CATALOG_DATABASE_URL` задан). |
| `google_sheets_sync`, `google_tasks_sync` | 1:1 зеркала задач. |

Имена таблиц для миграции: `alembic/versions/*.py`.

### Миграции

`alembic.ini` в корне. Создать миграцию: `alembic revision --autogenerate -m "..."`. Накатить: `alembic upgrade head`. На проде накатывается через `docker exec manager-bot-1 alembic upgrade head` или автоматически при старте контейнера (зависит от Dockerfile/entrypoint — проверь перед изменением схемы).

---

## 4. Domain pipelines — ключевые

### 4.1 Agenda (`app/agenda/`)

Daemon-тред `AgendaRunner` (запускается из `app/main.py`, интервал `AGENDA_TICK_INTERVAL_SECONDS`, дефолт 1800с).

⚠️ **`normalise_title(title)` в `service.py`** — ключ матчинга prior recordings. Все «Design Status» события группируются по нему. Не path/calendar_event_id! Если митинг переименовали — связь с историей рвётся.

⚠️ **TZ:** `scheduled_start_at` хранится в UTC; `_ddmm(dt)` в `slack_format.py` форматит по этому же UTC. Заголовок «03/06» может относиться к митингу 02/06 22:30 UTC, если он попадает на 03/06 в локали оператора. При дебаге сверяй `scheduled_start_at` напрямую.

Поток на тик:
1. `service.build_candidates()` — берёт upcoming события из Google Calendar, фильтрует organizer == operator, требует ≥ `min_prior_meetings` prior recordings с тем же `normalise_title`.
2. Для каждого кандидата: `find_prior_recordings()` → список Zoom-записей с тем же нормализованным title, newest first. Plus `is_transcript_unsummarizable` фоллбэк выбирает первую «не thin» запись для tasks (`agenda_prior_skipped_thin` лог).
3. `compose_agenda()` → ВСЕГДА `_compose_lite()` (FR-CR-05-192u: «никакого LLM»). Берёт `prior_recordings[0]`, strip'ает `<a href>...</a>`, строку `Участники:` и блок `TODO:` → получает `previous_recap`.
4. `runner.py:384-401` — если `previous_recap` пуст И `tasks_checklist` пуст И `open_questions` пуст → `agenda_skipped_empty_body`, не постим.
5. `slack_format.render_agenda_text()` рендерит топ-сообщение, `render_agenda_task_thread_replies()` — реплаи в тред с задачами.
6. Idempotency-row в `meeting_agendas`.

⚠️ **Известный баг (2026-06-03):** `_compose_lite` берёт `prior_recordings[0]` слепо. Если today's recording уже есть в БД, но summarizer провалился (`short_summary=""`), то thin-фоллбэк из шага 2 спасает только tasks, а recap всё равно становится пустым. Воспроизведено на «03/06 - Design Status». Фикс — зеркалить thin-фильтр в `_compose_lite`.

### 4.2 Fireflies (`app/fireflies/pipeline.py`)

Опрос API → транскрипт → bilingual detect → LLM summary (short + detailed) → tasks. Поток сидит в одном большом `pipeline.py` (~2000 строк), prompts вынесены в `prompts.py`. Постит в Slack thread, мирорит в `app/services/slack_mirror.py`.

### 4.3 Zoom (`app/zoom/pipeline.py`)

Похоже на Fireflies, но: скачивает аудио → Whisper → транскрипт. Падает с `'transcribe failed: Whisper empty + no VTT fallback'`, если запись «тихая». Calendar attendees реконсилируются: показываем только пришедших.

### 4.4 Counterparty Briefs (`app/counterparty_briefs/runner.py`)

Daemon. Сканит upcoming митинги, выделяет внешние организации, генерит Google Doc через LLM. Бюджет: `COUNTERPARTY_BRIEFS_LLM_BUDGET_USD`.

### 4.5 CEO Brain (`app/ceo_brain/`)

**Отдельный Slack-app в другом workspace** (`CEO_BRAIN_WORKSPACE_ID`). Архивирует каналы в `slack-archive/<channel>/YYYY-MM-DD.jsonl` + Postgres. Отвечает по @mention через Claude API.

---

## 5. Конфигурация

`app/config.py` — Pydantic `Settings`. Все env-vars там с `alias=`. Найди переменную: `grep -n "alias=\"FOO\"" app/config.py`.

Категории (неполный список):
- **Slack**: bot/app/signing tokens, scopes.
- **LLM**: `LLM_PROVIDER` (auto/openai/anthropic/none).
- **Agenda**: `AGENDA_ENABLED`, `AGENDA_SLACK_TARGET_CHANNEL_ID`, `AGENDA_TICK_INTERVAL_SECONDS` (1800), `AGENDA_TASKS_FROM_LAST_PRIOR_ONLY` (true), `AGENDA_TASK_FILTER_DIRECTIONS_DISABLED` (false). Escape-hatch: ставь false/true env'ы для отката поведения.
- **Briefs**: `COUNTERPARTY_BRIEFS_ENABLED`, `COUNTERPARTY_BRIEFS_LOOKAHEAD_DAYS` (7), `COUNTERPARTY_BRIEFS_LLM_BUDGET_USD` (2.0).
- **CEO Brain**: `CEO_BRAIN_ENABLED`, `CEO_BRAIN_ANTHROPIC_API_KEY`, `CEO_BRAIN_WORKSPACE_ID`.
- **Vector**: `CATALOG_DATABASE_URL`, `TASK_VECTOR_DATABASE_URL`.
- **Feature flags**: `COUNTERPARTY_MATCH_V2_MODE` (shadow/on/off), `TELEGRAM_ENABLED`, `FIREFLIES_ENABLED`, `ZOOM_ENABLED`, `SLACK_INGEST_ENABLED`.

`.env` лежит в корне (`~/manager/.env`). Внутри контейнеров переменные передаются через docker-compose `environment:`.

---

## 6. Спеки

Все живут в корне.

| Файл | Скоуп |
|---|---|
| `SPEC.md` | 534 KB. Большой agg-док. Считай неактуальным sliding source-of-truth — лучше открывать тематические. |
| `SPEC_v0.1.md` | Core: US-1 пассивный детект задач, US-2 explicit-действия, US-3 persistence + sync. |
| `docs/archive/SPEC_TASK_EXTRACTOR_v0.1.md` | Intent + извлечение полей. **🗄️ архив 2026-06-05** — net-new (Favorites, chat-subscription, blocked) не построен; реальный код трассируется через `FR-CR-05-*`. |
| `docs/archive/SPEC_TASK_TRACKER_v0.1.md` | Жизненный цикл задачи: create → confirm → status → Google sync. **🗄️ архив 2026-06-05** — спека сильно отстала; код ушёл вперёд (Slack-ingest / дайджесты / Google Tasks 2-way реализованы, но в спеке помечены TODO). См. `AUDIT.md` §6. |
| `SPEC_STATUS_TRACKER_v0.2.md` | Status updates из Zoom+FF → Sheets, поверх движка Task Vector (заменяет v0.1, которая в `docs/archive/`). |
| `SPEC_MEETING_AGENDA_v0.1.md` | Pre-meeting агенда. |
| `SPEC_NOTE_TAKER_v0.1.md` | Извлечение задач из транскриптов. |
| `SPEC_COUNTERPARTY_BRIEFS_v0.1.md` | Pre-meeting research-doc. |
| `SPEC_CEO_BRAIN_BOT_v0.1.md` | Архив + Claude-ответчик. |
| `CEO_BRAIN_SPEC_ASIS.md` | Снимок «как сейчас» CEO Brain. |
| `Spec_eng.md` / `.pdf` | Английская версия общей спеки. |

В коде ищи `FR-CR-05-XXX` маркеры — это операторские pinned-решения с датой; не трогай поведение без понимания контекста.

---

## 7. Полезные ops-скрипты

Запуск: `docker exec -it manager-bot-1 python -m ops.<script>` (или `python ops/<script>.py` из корня с активированным venv).

| Скрипт | Польза |
|---|---|
| `verify_today.py` | End-to-end QA дневных постов (агенды, брифы, attendees, bilingual). |
| `full_microcheck_table.py` | QC-матрица по митингам: транскрипт-длина, summary, attendees. Печатает таблицу + CSV. |
| `verify_full_19_21.py` | Запустить полный пайплайн на date range, дамп в markdown. |
| `agenda_update_recap.py` | **Постфактум-патч уже отправленной агенды**: подменить строку «На прошлой встрече: ...» когда summary прошлой встречи доехал поздно. |
| `quality_checklist.py` | Spot-check соответствия FR-CR-05-XXX. |
| `cluster_merge_review.py` | Ревью предложенных мерджей в `entity_catalog`. |
| `shadow_report.py` | Сравнение v1 vs v2 counterparty matching. |
| `sheet_sync_export_tasks.py` | Дамп задач в Google Sheets. |
| `slack_ingest_dryrun.py` | Dry-run Slack history ingest без сохранения. |

---

## 8. Не-очевидное (что я узнал на собственных граблях)

1. **`DATABASE_URL` на хосте не выставлен** и в `~/manager/.env` его НЕТ — Postgres только внутри docker network. Заходи через `docker exec -i manager-db-1 psql -U postgres -d slack_tasks`.
2. **Два бота** (`manager-bot-1` + `manager-zoom-ff-1` aka v2shadow) пишут в **одну** БД. v2shadow гоняет Zoom/FF пайплайны через `ops/v2_publish_meeting.py`.
3. **Две таблицы записей по источнику:** `zoom_recordings` (ключ `zoom_id`) для Zoom, `meeting_recordings` (ключ `fireflies_id`) для Fireflies. Это РАЗНЫЕ источники, не legacy/v2. Ищешь митинг — `title ILIKE` по обеим. И помни: провальная Zoom-запись (Whisper пустой) может существовать недолго и быть вычищенной — в логах `agenda_prior_skipped_thin` она есть, а в БД уже нет.
4. **Three engines** в `app/db.py`. Vector БД могут быть отдельным инстансом; fallback chain прописан в `task_vector_db.py`.
5. **Shadow v2 паттерн** для counterparty match: v1 = canonical, v2 = parallel, логирует mismatches, никогда не raise'ит.
6. **`compose_agenda` всегда lite, никакого LLM** (FR-CR-05-192u). Прошлый LLM-путь сохранён как `_compose_agenda_llm_legacy` — не вызывается.
7. **Bilingual restorer** (`app/services/bilingual_restorer.py`): если транскрипт <3% кириллицы — повторно фетчит источник и достаёт кириллицу.
8. **Daemon-треды не валят бота** при exception — просто пишут лог. Если daemon тихо умер — `docker logs` покажет stack-trace, а `agenda_tick_summary` перестанет тикать.
9. **`AGENDA_TASKS_FROM_LAST_PRIOR_ONLY=true`** (дефолт): open_tasks берутся только из последнего годного prior, не из всех. Иначе для Fundraising daily получаем 469 задач.
10. **Direction filter (`AGENDA_TASK_FILTER_DIRECTIONS_DISABLED`)**: задачи с `extra.direction='other'` или без direction не попадают в агенду. Если задача внезапно «исчезла» из агенды — проверь `extra.direction`.

---

## 9. Бранчинг

Активная ветка для текущей сессии — указана в системном prompt'е. **Всегда** работаешь на ней, никогда не пушишь в `main` без явного разрешения.

Коммиты по-английски, `git-log`-стиль (`Add`/`Fix`/`Refactor` + почему). Не амендим — каждый шаг отдельный коммит.

---

## 10. Когда что-то не работает

1. **Сначала логи:** `docker logs --since 24h manager-bot-1 | grep <event_name>`.
2. **Потом БД:** `docker exec -i manager-db-1 psql ...` с прицельным WHERE.
3. **Поведение pipeline'а — в коде**, не в спеках. Спеки иногда отстают от FR-CR-05-XXX операторских патчей.
4. **Не пиши новых docs/.md файлов** без явной просьбы пользователя — только редактируй существующие или этот AGENTS.md.
