# Handover — Slack Task Manager (ветка FR-CR-05)

> **Кому:** инженеру, который продолжает поддержку проекта.
> **Дата:** 2026-06-05.
> **Ветка:** `claude/slack-bot-task-extraction-9eGSC` (последний коммит запушен в `andre-kuzminykh/manager`).

Этот документ содержит:
1. Что изменилось на этой ветке и зачем (FR-CR-05-257/258).
2. Как это деплоить в боевой контейнер.
3. Карта окружения и точки входа.
4. Регламент работы (спека → тесты → код).

Все детали по архитектуре и оперативке — в `AGENTS.md`, `docs/TECHNICAL_ARCHITECTURE.md`,
`docs/PIPELINE_FLOW.md`, `AUDIT.md`, `PRD.md` и `SPEC_*.md` в корне репы.

---

## 1. Что изменилось на этой ветке

### 1.1 FR-CR-05-258 — Postgres advisory-лок per-meeting

**Проблема:** always-on раннер (`ops/zoom_fireflies_runner.py`) и ручные
ops (`ops/republish_meeting.py`) могли одновременно обработать одну и ту же
запись. Идемпотентность раннера букмарк-based с TOCTOU-окном: он читает
`tasks_extracted`/`last_error` в одной сессии, обрабатывает в другой. Если
ручной republish стартует во время тика крона → встреча обрабатывается дважды
→ дубли Google Doc, Slack-mirror и n8n-вебхука.

**Фикс:** новый модуль `app/services/pg_lock.py` с `try_meeting_lock(session,
source, source_id)` — берёт `pg_try_advisory_xact_lock` (auto-released при
коммите/роллбэке `session_scope`). Подключён в **трёх местах**:
- `ops/zoom_fireflies_runner.py` — оба цикла (FF + Zoom).
- `ops/republish_meeting.py` — после загрузки строки, выходит с кодом 3
  и понятным сообщением, если лок занят.

Best-effort: если БД упала при взятии лока — возвращаем True, чтобы инфра-
инцидент не блокировал работу.

### 1.2 FR-CR-05-257 — флаги идемпотентности

**Проблема #1.** В `_send_short_summary` (и Zoom, и Fireflies) флаг
`short_summary_sent` ставился ТОЛЬКО при успешной отправке Telegram-DM
(`sent > 0`). А Slack-mirror и n8n-вебхук уже были отправлены до этого
момента. Если TG падал (блокировка, лимит, error) → флаг оставался False →
следующий тик переотправлял ВЕСЬ хвост → дубли в Slack + повторный вебхук.
**Решено:** ставим `short_summary_sent = True` всегда после mirror+webhook,
независимо от исхода TG. Мёртвое раннее присваивание удалено.

**Проблема #2.** В Zoom `process_one` не было `already_processed`
short-circuit (асимметрично с Fireflies). Любое внешнее обнуление флага
(например, `republish --regenerate` или вручную проставленный `last_error`
на уже-обработанной строке) приводило к повторному входу в хвост и
дублированию Slack/вебхука/task-карточек. **Решено:** добавлен такой же
guard, как в FF — проверяет 7 флагов и выходит с `skipped_reason="already_processed"`.

### 1.3 Тесты

- `tests/requirements/test_pg_meeting_lock.py` — 5 тестов: детерминизм
  ключа, разные ключи для разных (source, id), acquired/held/error-paths.
- `tests/requirements/test_zoom_already_processed_guard.py` — 2 теста:
  полностью обработанная строка скипается БЕЗ LLM-вызовов; недо-обработанная
  не короткозамыкается.
- `tests/requirements/test_retry_cap_and_sentinels.py` — два MagicMock-теста
  доправлены (`MagicMock(spec=ZoomRecording)` делал все step-флаги
  truthy → новый guard их считал "fully processed"). Поставили
  `processed_at=None` — строка на attempts-cap по определению не fully processed.

Все 12 затронутых тестов зелёные.

### 1.4 Прочие коммиты этой же ветки

```
docs: HANDOVER.md (этот файл)
FR-CR-05-257/258: stop duplicate Slack/Doc/webhook deliveries
republish_meeting: --reexport-doc (no-LLM Doc refresh after --rename)
republish_meeting: --rename for deterministic point-fix of names
FR-CR-05-254 calendar-only attendees + FR-CR-05-256 canon single-pass
FR-EC-CRITIC-2: local replica of CRM catalog + daily sync
FR-EC-CRITIC-2: shard FR catalog by record count (~200) + critic
owner matching: raise notes cap 200 -> 1000 chars per employee
republish_meeting: --sheet-only + --sheet-id (read-reconcile by task id)
republish_meeting: Zoom support + Google Sheet task sync
```

Поверх этой ветки можно мержить в main.

---

## 2. Развёртывание

### 2.1 Топология (см. AGENTS.md §50-60)

| контейнер              | образ                  | что делает                                      |
|------------------------|------------------------|-------------------------------------------------|
| `manager-db-1`         | `postgres:16-alpine`   | основная БД `slack_tasks`                       |
| `manager-bot-1`        | `manager-bot[:latest]` | Slack-bot (Socket Mode), TG-handlers            |
| `manager-zoom-ff-1`    | `manager-bot:v2shadow` | **актуальный** Zoom/Fireflies polling-runner    |

Прод **не** через `docker-compose up` — оба бота запущены через `docker run`
с env инлайн. Файл `docker-compose.yml` в репе — только локальный dev.

### 2.2 После любого изменения раннера

```bash
# 1. Залить файлы на хост
git pull в ~/manager-zff (если он git-checkout) или скопировать вручную

# 2. Если поменялся ИМПОРТ (новый модуль app.services.pg_lock — да, поменялся):
docker build -t manager-bot:v2shadow .

# 3. Перезапустить:
docker restart manager-zoom-ff-1

# 4. Проверить, что код реально новый:
docker exec manager-zoom-ff-1 grep -c "FR-CR-05-258" /app/ops/zoom_fireflies_runner.py
# должно быть 2

# 5. Логи:
docker logs --since 10m -f manager-zoom-ff-1
# искать строки: zoom_ff_runner_starting, ff_runner_tick, zoom_runner_tick,
# и НОВЫЕ: ff_runner_skip_locked, zoom_runner_skip_locked, pg_advisory_lock_error
```

**ВАЖНО про "in-memory old code":** в проде раннер монтируется с хоста
(`/home/andre/manager-zff/zoom_fireflies_runner.py`), но **Python-процесс не
перечитывает файл сам**. После `docker cp` или редактирования файла на хосте
**обязателен `docker restart manager-zoom-ff-1`**, иначе процесс продолжит
крутить старый код в памяти. Самая обидная отладочная западня этой репы.

### 2.3 Миграции

```bash
docker exec manager-bot-1 alembic upgrade head
```

На этой ветке новых миграций НЕТ (последняя — `0042_fr_catalog_snapshots`,
уже в `main`). Если катить ветку поверх — миграции уже накатаны.

---

## 3. Карта окружения

### 3.1 Где что лежит

| компонент                         | путь                                                |
|-----------------------------------|-----------------------------------------------------|
| Slack-bot entry                   | `app/main.py` → `app.slack_bot.app.build_app`       |
| Zoom/FF polling-runner            | `ops/zoom_fireflies_runner.py`                      |
| Ручной republish одной встречи    | `ops/republish_meeting.py`                          |
| Zoom pipeline                     | `app/zoom/pipeline.py` (`ZoomPipeline.process_one`) |
| Fireflies pipeline                | `app/fireflies/pipeline.py` (`FirefliesPipeline.process_one`) |
| Entity resolver FR (CRM-каталог)  | `app/services/entity_resolver_fr.py`, `app/services/fr_catalog_replica.py` |
| Канонизация имён                  | `app/services/counterparty_match.py:canonicalize_text` |
| Sheet sync (System A — активный)  | `app/sync/sheets.py`                                |
| Sheet sync (System B — дормант)   | `app/sheet_sync/*`                                  |
| Календарь / attendees             | `app/services/calendar_attendees.py`                |
| Slack mirror                      | `app/services/slack_mirror.py`                      |
| n8n meeting webhook               | `app/services/meeting_webhook.py`                   |
| Advisory-локи                     | **`app/services/pg_lock.py`** (новый, FR-CR-05-258) |
| Конфиг (Pydantic Settings)        | `app/config.py`                                     |
| Модели                            | `app/models/` (`zoom.py`, `fireflies.py`, …)        |
| Миграции                          | `alembic/versions/0001..0042_*.py`                  |

### 3.2 Где смотреть оперативку

```bash
# Логи раннера за сутки + грэп по тику:
docker logs --since 24h manager-zoom-ff-1 2>&1 | grep -E "zoom_runner_tick|ff_runner_tick|skip_locked|already_processed"

# Сколько встреч обработано:
docker exec -i manager-db-1 psql -U postgres -d slack_tasks <<'SQL'
SELECT date_trunc('hour', processed_at) AS h, COUNT(*)
FROM zoom_recordings WHERE processed_at > now() - interval '24 hours'
GROUP BY 1 ORDER BY 1;
SQL

# Что висит с last_error:
docker exec -i manager-db-1 psql -U postgres -d slack_tasks -c \
  "SELECT zoom_id, title, last_error FROM zoom_recordings WHERE last_error IS NOT NULL ORDER BY updated_at DESC LIMIT 20;"
```

### 3.3 Доки в репе (читать в этом порядке)

1. `AGENTS.md` — топология контейнеров, env-инжект, как ходить в БД.
2. `docs/PIPELINE_FLOW.md` — пошагово, как одна встреча проходит pipeline.
3. `docs/TECHNICAL_ARCHITECTURE.md` — общая архитектура + ID точек входа.
4. `PRD.md` — фичи и их статус.
5. `AUDIT.md` — расхождения спека ↔ код по каждой фиче (на дату 2026-06-03).
6. `SPEC_*.md` (в корне) — спеки фичей. Имена FR в коде — `FR-CR-05-*`,
   а в спеках свои (`FR-NT-*`, `FR-EC-*` и т.п.); сверка — в `AUDIT.md`.
7. `docs/FR_TEST_MATRIX.md` — какие тесты покрывают какой FR.

---

## 4. Регламент работы (как принято в этой репе)

Это **не формальность**, а то, что чинит большинство багов до их появления:

1. **Спека → тесты → код.** Перед изменением логики:
   - найди (или напиши) FR в `SPEC_*.md`,
   - открой/добавь тест в `tests/requirements/`,
   - **запусти тест и убедись, что он КРАСНЫЙ**,
   - только потом меняй прод-код,
   - тест должен стать зелёным.
2. **Один коммит = один FR-CR-05-NNN.** Номер инкрементируется (текущий
   максимум — 258). В сообщении коммита и в комментариях к изменению.
3. **Комментарии в коде — только "WHY".** Что код делает — видно по коду;
   почему он делает именно так (и какой регрессии не было до этого) —
   обязательный комментарий.
4. **Раннер — НЕ перезагружает код.** Любая правка кода, который выполняет
   `manager-zoom-ff-1` → `docker restart manager-zoom-ff-1` обязателен.
   Иначе ты будешь видеть "правильный" код в файле и "старое" поведение
   в логах.
5. **DB-локи.** Любая фоновая обработка одной "сущности" (recording, task,
   meeting) — оборачивай в `try_meeting_lock` или аналогичный
   `pg_try_advisory_xact_lock`. См. `app/services/pg_lock.py` как шаблон.
6. **Не амендить коммиты, не пушить force.** `--no-verify` тоже не нужен.

---

## 5. Финальный чек-лист

- [ ] Образ `manager-bot:v2shadow` пересобран с `app/services/pg_lock.py`.
- [ ] `manager-zoom-ff-1` перезапущен; в логах виден
      `ff_runner_started` + (когда сработает) `ff_runner_skip_locked` / `zoom_runner_skip_locked`.
- [ ] Прогнан тест-пак: `python -m pytest tests/requirements/test_pg_meeting_lock.py tests/requirements/test_zoom_already_processed_guard.py -q` → зелёное.
- [ ] Ветка `claude/slack-bot-task-extraction-9eGSC` доступна в `andre-kuzminykh/manager`.
- [ ] Этот `HANDOVER.md` прочитан целиком.
