# SPEC v0.1 — Status Tracker (Zoom + Fireflies → Sheets)

> **🗄️ Архивная редакция (2026-06-05).** Заменена на актуальную
> [`SPEC_STATUS_TRACKER_v0.2.md`](../../SPEC_STATUS_TRACKER_v0.2.md) в корне репы.
> Функциональные требования v0.1 в силе, изменилась только техника
> (переиспользование движка Task Vector). Сохранено как исторический референс.

> **Дата:** 2026-06-03
> **Скоуп v1:** Zoom + Fireflies → обновление статуса/комментария в Google Sheets
> **Не в скоупе v1:** Telegram, Slack, reopen done/cancelled, review queue, ручной апрув
> **Связанные доки:** `SPEC_TASK_EXTRACTOR_v0.1.md` (создание задач), `SPEC_TASK_TRACKER_v0.1.md` (общая концепция)

---

## 1. Задача

Сотрудники упоминают задачи на встречах («отправил Mark коммерческое», «закрыли с юристами», «по Acme жду ответа»). Сейчас это растворяется в транскриптах. Нужно:

1. Слушать Zoom + Fireflies.
2. Привязывать упоминание к существующей живой задаче в трекере.
3. Авто-обновлять Sheet: `status`, `last_update_date`, `last_comment`, `last_update_source`.
4. Логировать всё append-only — даже комментарии без смены статуса.

Целевая метрика v1: ≥60% живых задач с last_update_date ≤ 7 дней без ручного ввода.

---

## 2. Решения (зафиксировано)

| # | Решение | Значение |
|---|---|---|
| D1 | Источники v1 | Zoom + Fireflies |
| D2 | Применение | Полный авто, без review queue |
| D3 | Sheet | 1 строка/задача, перезапись |
| D4 | Комментарии без статуса | Логируются, обновляют `last_comment` + `last_update_date` |
| D5 | Чьи упоминания учитываем | О чужих задачах тоже (any speaker, any assignee) |
| D6 | Окно матчинга | Только живые: `todo`, `in_progress`, `backlog` |
| D7 | Fireflies гранулярность | Один проход LLM по полному транскрипту, пост-фактум |
| D8 | Модель экстракции | `claude-sonnet-4-6` (TBD), reasoning=high |

---

## 3. Контракт события (canonical)

Append-only лог, одна строка = один атомарный апдейт о задаче.

```json
{
  "event_id": "uuid",
  "source": "fireflies | zoom",
  "source_ref": {
    "meeting_id": "ff_abc123",
    "meeting_title": "Weekly sync 2026-06-03",
    "segment_idx": 42,
    "ts_start_sec": 1830,
    "ts_end_sec": 1842
  },
  "speaker": {
    "raw_name": "Семён Иванов",
    "person_id": "p_semyon"
  },
  "task_id": "t_8821",
  "task_assignee_id": "p_semyon",
  "new_status": "done | in_progress | blocked | null",
  "comment": "Отправил Mark коммерческое",
  "raw_quote": "Я вчера отправил Марку коммерческое, жду ответа",
  "confidence": 0.91,
  "extracted_at": "2026-06-03T11:42:18Z"
}
```

Правила:
- `new_status == null` → comment-only ивент (D4).
- `task_assignee_id` может ≠ `speaker.person_id` (D5).
- Идемпотентность: PK = `(source, source_ref.meeting_id, source_ref.segment_idx, task_id)`. Повтор → no-op.

---

## 4. Pipeline

```
Fireflies webhook (transcript.completed)
        │
        ▼
┌──────────────────────┐
│ 1. Ingest            │  скачать транскрипт + speaker map
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ 2. LLM Extract       │  один проход, sonnet-4-6 high
│                      │  output: candidates[]
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ 3. Identity resolve  │  speaker.raw_name + mentioned_name
│                      │       → person_id (alias table)
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ 4. Task matcher      │  candidate → t_id среди живых задач
│                      │  по {assignee, object/keywords}
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ 5. Event log (DB)    │  append, with idempotency check
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ 6. Projector → Sheet │  upsert row, last write wins by ts
└──────────────────────┘
```

Zoom — тот же пайплайн, источник транскрипта другой (Zoom Cloud Recording → transcript ready webhook).

---

## 5. LLM Extraction

**Input:** транскрипт с тайм-кодами и спикерами + список живых задач (id, title, assignee).

**Prompt skeleton:**
> Ты слушаешь рабочую встречу. Найди фрагменты, где говорят о прогрессе по конкретной задаче из списка. Для каждого верни: speaker, mentioned_assignee_hint, task_id_guess (или null), status_hint (done/in_progress/blocked/comment), quote (одна реплика), confidence (0..1).
> Игнорируй: small talk, обсуждение будущих задач без статуса, чужие компании без привязки.

**Output schema:** JSON array of candidates. Без чёткого `task_id_guess` — кандидат всё равно проходит дальше (матчер попробует найти).

**Reasoning level:** high (D8). Стоимость на один час встречи ≈ 1 LLM call, ок.

---

## 6. Task matching

Вход: candidate с `(mentioned_assignee_hint, status_hint, quote)`.

Алгоритм:
1. Кандидатное множество = живые задачи (D6).
2. Если есть `mentioned_assignee_hint` → отфильтровать по assignee. Иначе всё множество.
3. Скоринг каждой задачи: cosine(embedding(quote), embedding(task.title + task.description)) + бонус за keyword overlap.
4. Top-1 score ≥ `THRESH_MATCH` (старт: 0.72) → match. Top-2 close (Δ < 0.05) → пропустить (ambiguous), залогировать.
5. Нет матча ≥ THRESH → drop ивент, метрика `unmatched_candidates_total++`.

Параметры (THRESH_MATCH, Δ, score weights) — в конфиге, тюним по факту.

---

## 7. Identity resolution

Таблица `person_aliases`:

| person_id | canonical_name | aliases |
|---|---|---|
| p_semyon | Семён Иванов | ["Сёма", "Semyon", "Semen I."] |

- Zoom display name / Fireflies speaker → matched через aliases.
- Mentioned имена в quote («сказал Олег») → тот же lookup.
- Нет матча → ивент пропускается с логом `unknown_speaker`.

Алиасы наполняются вручную в v1 (отдельный лист в том же Sheet).

---

## 8. Sheet schema

Лист `tasks`:

| Колонка | Тип | Источник |
|---|---|---|
| task_id | str | from extractor / трекер |
| title | str | трекер |
| assignee | str (canonical_name) | трекер |
| status | enum: todo/in_progress/blocked/done/cancelled | last event projection |
| last_update_date | ISO datetime | last event ts |
| last_update_source | enum: fireflies/zoom/manual | last event source |
| last_comment | str (≤500 char) | last event comment or quote |

Проекция: `ORDER BY extracted_at DESC LIMIT 1` для каждой task_id → write row. Done/cancelled не перезаписываются обратно в open (D6).

---

## 9. Хранилище

- **Event log:** Postgres таблица `task_status_events` (append-only, индексы на task_id, extracted_at).
- **Sheet:** проекция через Google Sheets API, идемпотентный upsert по task_id.
- **Aliases:** отдельный лист `aliases` в том же Spreadsheet, читается раз в N минут в кэш.

---

## 10. Failure modes

| Случай | Поведение |
|---|---|
| LLM не нашёл ни одного кандидата | OK, метрика `meetings_without_candidates` |
| Кандидат без task_id_guess и matcher не нашёл | Drop, лог `unmatched` |
| Speaker неизвестен | Drop, лог `unknown_speaker` |
| Mentioned assignee неизвестен | Падаем на speaker как assignee, лог `unknown_mention` |
| Match попал в done/cancelled задачу | Skip, лог `match_on_closed` (мониторим — если часто, пересматриваем D6) |
| Sheets API недоступен | Ретрай с экспонентой, событие уже в DB, проектор подхватит |
| Дубликат вебхука | Idempotency key срабатывает, no-op |

---

## 11. Метрики (Day-7 dashboard)

- `events_extracted_total{source}` — счётчик ивентов.
- `events_applied_total` / `events_dropped_total{reason}`.
- `match_confidence_distribution` — гистограмма.
- `tasks_with_fresh_update_pct` — % живых задач с last_update_date ≤ 7d. Главная метрика.
- `false_positive_rate` — ручная выборка 20 ивентов/неделя, проверяем глазами.

Цель Day-30: tasks_with_fresh_update_pct ≥ 60%, false_positive_rate ≤ 10%.

---

## 12. План выкатки

| Этап | Что | Гейт перехода |
|---|---|---|
| M1 | Fireflies ingest + LLM extract + лог в DB (без Sheet writeback) | 10 встреч прогнано, кандидаты глазами норм |
| M2 | Matcher + Sheet writeback в shadow-sheet | 1 неделя, false positive ≤ 15% |
| M3 | Переключить на боевой Sheet | Метрика стабильна |
| M4 | Добавить Zoom (тот же пайплайн, другой ingest) | M3 стабилен 1 неделю |
| M5+ | Telegram, Slack | вне v1 |

---

## 13. Открытые вопросы

- Точная модель + цена за час Fireflies (зависит от средней длины встречи; пилот покажет).
- Embedding-модель для matcher (start: `text-embedding-3-small`, может OK).
- Где живёт alias-таблица: тот же Sheet или отдельная админка? — в v1 Sheet, в v2 пересмотрим.
- Что делать с серией событий по одной задаче за одну встречу (несколько реплик подряд)? — collapse в один ивент с конкатенированным quote на этапе LLM-экстракции.
