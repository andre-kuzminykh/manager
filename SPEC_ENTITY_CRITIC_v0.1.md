# SPEC v0.1 — Entity critic: multi-sheet retrieval + context selection (FR-EC-CRITIC)

> **Дата:** 2026-06-04 · **Эпик:** `FR-EC-CRITIC` (entity selection critic)
> **Флаги:** `ENTITY_CRITIC_ENABLED`, `ENTITY_CRITIC_SHADOW` (оба default false).
> **Связано:** [SPEC_ENTITY_CONSISTENCY_v0.1.md](./SPEC_ENTITY_CONSISTENCY_v0.1.md)
> (FR-CR-05-242 — одна форма на встречу), [AUDIT.md](./AUDIT.md) §entity.
> **Статус:** ⚪ SPEC-ONLY (код не написан; ingest + критик за флагами, shadow-first).

## 1. Проблема

Начальница (Ира) пользуется ботом коллеги (Виктор) и он отдаёт **правильные**
канонические имена там, где наш бот ошибается («большая часть была в файле
followers»). Наш резолвер (`entity_match_v2`: vector top-k + fuzzy canonicalize)
проигрывает по **recall** (если имени нет в нашем `counterparties`-Sheet — не найдём)
и по **точности выбора** (между двумя похожими кандидатами из разных списков мы
берём ближайшего по строке, а не уместного по контексту встречи).

## 2. Гипотеза о подходе Виктора (подтвердить зондом MCP)

Это **RAG c финальным критиком**, не один список в промпт:

1. **Источники — все его листы** (`followers`, `List_Series_A`,
   `Xasis (European SFO Targets)`, …), у каждой сущности атрибуты:
   `type / status / industry / next_action / last_update` (видно в
   `humanoid_fr_search`).
2. **Retrieval** кандидатов по упоминанию из всех листов.
3. **Финальный критик** (LLM): упоминание + **контекст встречи** + кандидаты с
   атрибутами → выбирает того, **кто лучше подходит под контекст**.

Чего у нас нет — именно шага (3) **с контекстом встречи**. Это и строим.

## 3. Решения

| # | Решение |
|---|---|
| D1 | Каталог пополняем из **всех листов** (не только counterparties), с провенансом листа и атрибутами. |
| D2 | Перед каждым ingest — **snapshot** текущего каталога (pg_dump) → откат одной командой. |
| D3 | Новый шаг-критик: retrieval кандидатов (vector+lex+alias, по всем листам) → **один LLM-вызов** с контекстом встречи → каноническое имя + лист + confidence. |
| D4 | Критик **за флагом** `ENTITY_CRITIC_ENABLED` (default false). При off — текущий `entity_match_v2` без изменений. |
| D5 | `ENTITY_CRITIC_SHADOW=true` — критик **считает и логирует** свой выбор, но НЕ применяет (сверяем с текущим перед раскаткой). |
| D6 | Применяем выбор критика только если `confidence ≥ порога` И он отличается от текущего. Иначе оставляем текущий резолв. |
| D7 | Каждое решение критика пишем в **аудит-лог** (`entity_critic_decisions`) — для сверки, метрик и отката формы постфактум. |

## 4. Архитектура

```
[ingest, разовый/по cron]
  все листы Виктора/наши Sheets ──► snapshot каталога (D2)
        │                            └─ pg_dump entity_catalog_staging → /traces/catalog_backup_<ts>.sql
        ▼
  upsert в entity_catalog_staging (+ source_list, +attrs)   ← аддитивно, по name_normalised

[runtime, шаг canonicalize встречи, за флагом]
  extracted entity mention
        │  retrieval: vector top-k + lexical + alias  (по всем листам)
        ▼
  candidates[]  (name, list, type, status, industry, last_update, score)
        │  + meeting context: title, participants, transcript-window вокруг упоминания
        ▼
  LLM-критик ──► { canonical, source_list, confidence, reason } | {none}
        │  (shadow: только лог; live: apply если confidence≥порог и ≠ current)
        ▼
  запись в entity_critic_decisions + (live) подстановка канонической формы
        │
        ▼
  далее — карта замен detailed → задачи/короткое (SPEC_ENTITY_CONSISTENCY, без изменений)
```

## 5. Данные (аддитивно, ничего не ломаем)

- **`entity_catalog_staging`** — добавить nullable-поля: `source_list TEXT`,
  `attrs JSONB` (status/industry/next_action/last_update). Старые строки = NULL,
  поведение vector-поиска не меняется (поля для критика, не для эмбеддинга).
- **`entity_critic_decisions`** (новая таблица): `id, meeting_source (zoom/ff),
  meeting_source_id, mention, chosen_name, chosen_list, confidence, reason,
  candidates JSONB, applied BOOL, shadow BOOL, created_at`. Append-only —
  для метрик recall/точности и разбора жалоб.
- Миграция additive `0041_entity_critic.py` (`down_revision=0040`).

## 6. Чистые / тестируемые юниты

- `assemble_candidates(mention, hits) -> list[Candidate]` — слияние vector/lex/alias,
  дедуп по name_normalised, сорт по score. (чистая)
- `build_critic_prompt(mention, context, candidates) -> str` — детерминированная сборка. (чистая)
- `parse_critic_response(text) -> Decision` — извлекает `{canonical, source_list,
  confidence, reason}` или `none`; на мусоре → `none` (fail-safe). (чистая)
- `should_apply(decision, *, current, min_confidence) -> bool` — `confidence≥min И
  canonical≠current И canonical непустой`. (чистая)

LLM-вызов изолирован за этими чистыми функциями — тестируем без сети.

## 7. Инварианты безопасности

- **I1.** `ENTITY_CRITIC_ENABLED=false` → ровно текущий `entity_match_v2`. Ноль изменений по умолчанию.
- **I2.** `ENTITY_CRITIC_SHADOW=true` → ничего не подставляется, только лог `entity_critic_decisions(shadow=true)`. Сверяем неделю.
- **I3.** Низкая `confidence` / `none` / ошибка LLM → fallback на текущий резолв. Имя всегда получается.
- **I4.** ingest НЕ трогает прод-таблицы (`counterparties`, `tasks`) — только `entity_catalog_staging`. Перед ingest — snapshot (D2).
- **I5.** Каждое применённое решение залогировано → форму можно откатить постфактум по `entity_critic_decisions`.

## 8. Бэкап и откат (по требованию оператора)

| Что | Бэкап | Откат |
|---|---|---|
| Спеки/код | git-ветка `claude/...`, версия `_v0.1` | `git revert` / `git checkout <tag>` |
| Каталог перед ingest | `pg_dump entity_catalog_staging` → `catalog_backup_<ts>.sql` (D2) | `psql < catalog_backup_<ts>.sql` |
| Применённые формы | `entity_critic_decisions` (append-only) | re-apply прежней формы по логу |
| Поведение рантайма | флаги off + shadow | снять `ENTITY_CRITIC_ENABLED` (env), без передеплоя кода |

## 9. Тесты

`tests/requirements/test_entity_critic.py`:
- `assemble_candidates` — дедуп/сорт/слияние источников;
- `build_critic_prompt` — содержит mention, контекст, всех кандидатов с атрибутами;
- `parse_critic_response` — валид/мусор/none;
- `should_apply` — порог, identity-skip, пустой-skip;
- shadow-режим: решение пишется, подстановка НЕ происходит (мок-LLM).

## 10. Раскатка

- **E0** — миграция additive (поля + `entity_critic_decisions`). Прод не меняется.
- **E1** — ingest всех листов в каталог (после snapshot). Сверить размер каталога до/после.
- **E2** — `ENTITY_CRITIC_SHADOW=true`: неделю собираем `entity_critic_decisions`, сверяем выбор критика с текущим резолвом и с ботом Виктора на тех же встречах.
- **E3** — `ENTITY_CRITIC_ENABLED=true` (shadow off): включаем подстановку. Метрика — доля задач/саммари с корректной канонической формой (жалобы Иры → 0).

## 11. Открытые вопросы (подтвердить зондом MCP Виктора с прод-хоста)

- Точный список листов и набор полей у сущности (`humanoid_fr_search` schema).
- Делает ли Виктор retrieval сам или кладёт весь список в промпт (влияет на E1: нужен ли нам vector или хватит lexical+LLM на 1.5К именах).
- Порог `min_confidence` (старт 0.7) — тюним по shadow-логу E2.
- Нужен ли критику transcript-window или хватает title+participants (стоимость vs точность).
