# SPEC v0.2 — Status Tracker (reconciled с Task Vector)

> **Дата:** 2026-06-03 · **Заменяет:** [SPEC_STATUS_TRACKER_v0.1.md](./SPEC_STATUS_TRACKER_v0.1.md)
> (функциональные требования v0.1 в силе; v0.2 меняет ТЕХНИКУ — переиспользование
> движка Task Vector + единый лог апдейтов + откат из мастер-таблицы).
> **Связано:** [docs/SPEC_TASK_VECTOR_v0.1.md](./docs/SPEC_TASK_VECTOR_v0.1.md) (FR-TV), [AUDIT.md](./AUDIT.md) §10.

## 0. Зачем v0.2

Аудит (2026-06-03) показал: v0.1 предлагал **свой** вектор-матчер (`text-embedding-3-small`,
THRESH 0.72) и **свой** резолв людей (`person_aliases`) — это **дубль** уже существующего движка
**Task Vector (FR-TV)**, у которого есть индекс задач (`text-embedding-3-large`), индекс команды
и write-path (Sheets/Tasks/карточки). v0.2: Status Tracker строится **поверх** Task Vector, а не
заново. Плюс жёсткое требование оператора: **каждый апдейт статуса логируется (что→на что), и из
мастер-таблицы можно откатиться.**

## 1. Архитектура: один движок, два входа, один лог

```
  ВХОД №1 (актив, чат)            ВХОД №2 (пассив, встречи)        ВХОД №3 (ручной)
  CEO Brain: «отправил X»         Zoom/FF транскрипт                правка в Google Sheet
        │                               │                                │
        │  Task Vector P6                │  Status Tracker extractor       │  sheet pull
        │  (vector match задачи)         │  (LLM: progress-события)        │
        └───────────────┬───────────────┴────────────────┬───────────────┘
                        ▼                                  ▼
              ┌──────────────────────────────────────────────────┐
              │  WRITE ENGINE (Task Vector, существует)           │
              │  match → validate → apply(status/due/owner/comment)│
              └───────────────────────┬──────────────────────────┘
                                      ▼
              ┌──────────────────────────────────────────────────┐
              │  task_status_events  (append-only, ЕДИНЫЙ лог)    │  ← источник истины для отката
              └───────────────────────┬──────────────────────────┘
                          ┌───────────┴───────────┐
                          ▼                       ▼
              tasks (current state)      Google Sheet: вкладка `status_log`
              + Google Tasks + карточки  (проекция лога) + основная вкладка (1 строка/задача)
```

## 2. Единый лог апдейтов — `task_status_events` (FR-ST-LOG)

Append-only. Пишется при ЛЮБОМ изменении задачи из ЛЮБОГО входа (включая откат).

| Поле | Тип | Назначение |
|---|---|---|
| `event_id` | uuid PK | |
| `task_id` | FK tasks | |
| `source` | enum: `chat` / `zoom` / `fireflies` / `sheet` / `rollback` | откуда апдейт |
| `actor` | str | кто (slack/tg user id или `system`) |
| `field` | enum: `status` / `due_date` / `owner` / `comment` | что менялось |
| `from_value` | str/null | прежнее значение (для отката) |
| `to_value` | str/null | новое |
| `comment` | str/null | свободный комментарий (D4: comment-only ивент) |
| `raw_quote` | str/null | цитата из транскрипта (для встреч) |
| `confidence` | float/null | уверенность матча (встречи) |
| `meeting_ref` | jsonb/null | {source_id, segment_idx, ts} |
| `applied` | bool | применено к задаче или только залогировано (ambiguous→false) |
| `created_at` | ts | |

- **FR-ST-LOG-1:** каждый успешный апд(status/due/owner/comment) пишет ровно одну строку с `from_value`/`to_value`.
- **FR-ST-LOG-2:** идемпотентность встреч: PK-уник `(source, meeting_ref.source_id, meeting_ref.segment_idx, task_id)`; повтор вебхука → no-op.
- **FR-ST-LOG-3:** существующий `TaskStatusHistory` остаётся для enum-переходов; `task_status_events` — суперсет (все поля, все источники, comment-only). Все write-пути Task Vector (`task_tools.py:update_task_status/due/owner`) дополнительно пишут сюда.

## 3. Откат из мастер-таблицы (FR-ST-RB)

- **FR-ST-RB-1:** откат = взять `from_value` нужного события → применить через **тот же** write-engine →
  записать НОВОЕ событие `source=rollback`, `to_value=восстановленное`. Append-only не нарушается.
- **FR-ST-RB-2:** доступ:
  - `ops/rollback_task_status.py --event-id <uuid>` (откат конкретного события) и `--task-id <id> --to <iso-ts>` (к состоянию на момент);
  - CEO-brain тул `undo_last_task_status(task_id)` (последний апдейт; добивает FR-TV MCP gap из аудита).
- **FR-ST-RB-3:** мастер-Sheet получает вкладку `status_log` = проекция `task_status_events`
  (task_id, field, from→to, source, actor, comment, ts) — оттуда видно историю и можно вызвать откат
  по event_id.

## 4. Вход №1 — Task Vector P6 (актив, чат)

Движок уже написан (`app/ceo_brain/task_tools.py`, за флагом `TASK_VECTOR_WRITES_ENABLED`). Доделать:
- **FR-ST-P6-1:** каждый `update_*` пишет в `task_status_events` (см. §2).
- **FR-ST-P6-2:** `ops/gen_task_vector_eval.py` — синтет-эвал (NFR-TV-004) для калибровки τ/δ.
- **FR-ST-P6-3:** калибровка порогов на эвале → флип `TASK_VECTOR_WRITES_ENABLED=true`.

## 5. Вход №2 — Status Tracker (пассив, встречи)

Функционал — по [v0.1](./SPEC_STATUS_TRACKER_v0.1.md) (D1–D8, §5–§11). Техника:
- **FR-ST-EX-1:** после `detailed_summary` — один LLM-проход (D7) извлекает progress-события
  `{speaker, mentioned_task_hint, status_hint, comment, quote, confidence}`.
- **FR-ST-MATCH-1:** матч кандидата к живой задаче — через **Task Vector** `search_tasks`
  (`entity_embeddings`, `text-embedding-3-large`), НЕ через свой 3-small индекс. Резолв людей —
  через team-индекс Task Vector, НЕ `person_aliases`.
- **FR-ST-APPLY-1:** при уверенном единственном матче (порог `TASK_VECTOR_TAU_HIGH`, ambiguity-skip по
  `TASK_VECTOR_DELTA`) — апдейт через write-engine; иначе залогировать событие `applied=false` (review).
- **FR-ST-APPLY-2:** авто-apply (v0.1 D2), но с порогом + skip-ambiguous + метрика `false_positive_rate`
  (v0.1 §11). Это компромисс с консервативностью FR-TV — авто только при высокой уверенности.

## 6. Не дублируем (явный список reuse)

| Нужно | Берём из | НЕ строим |
|---|---|---|
| матч фразы→задача | Task Vector `search_tasks` (`entity_embeddings`) | свой `text-embedding-3-small` индекс |
| резолв людей | Task Vector team-индекс | `person_aliases` таблицу |
| применение апдейта | Task Vector `update_*` + `task_sync` | свой Sheet-writer |
| undo | Task Vector `{field,from,to}` + §3 | — |

## 7. План выкатки

| Этап | Что | Гейт |
|---|---|---|
| S0 | `task_status_events` (миграция+модель) + проекция в Sheet `status_log` + `ops/rollback_task_status.py` | спайн готов, юнит-тесты зелёные |
| S1 | P6: write-пути пишут в лог + `gen_task_vector_eval` + калибровка → флаг writes on | оператор апдейтит задачи из чата, всё в логе, откат работает |
| S2 | Status Tracker extractor + авто-apply (reuse матчер/люди/движок) в shadow | 10 встреч, FP ≤ 15% |
| S3 | боевой авто-apply | метрика стабильна |

## 8. Тесты (traceability)

- `test_status_events_log.py` — каждый апдейт пишет from→to; comment-only; идемпотентность встреч.
- `test_status_rollback.py` — откат восстанавливает значение и пишет `source=rollback` событие.
- `test_status_tracker_extract.py` — LLM-кандидаты → матч через Task Vector → apply/skip по порогу.
- переиспользуют моки Task Vector (`tests/requirements/test_task_vector_tools.py`).
