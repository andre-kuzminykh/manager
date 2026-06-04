# SPEC v0.1 — Entity quality via Viktor's fundraising DB (FR-EC-CRITIC)

> **Дата:** 2026-06-04 · **rev2** (переписано после разбора n8n-воркфлоу Виктора)
> **Эпик:** `FR-EC-CRITIC` (entity quality / canonical selection)
> **Флаги:** `ENTITY_FR_MIRROR_ENABLED`, `ENTITY_CRITIC_ENABLED`, `ENTITY_CRITIC_SHADOW` (все default false).
> **Связано:** [SPEC_ENTITY_CONSISTENCY_v0.1.md](./SPEC_ENTITY_CONSISTENCY_v0.1.md) (одна форма на встречу).
> **Статус:** ⚪ SPEC-ONLY. Бэкап прежней редакции spec — в git (`7d71e56`).

## 1. Проблема

Начальница (Ира) пользуется ботом коллеги (Виктор) — он отдаёт **правильные**
канонические имена там, где наш бот ошибается («большая часть была в файле followers»).

## 2. Как РЕАЛЬНО устроено у Виктора (из его n8n — факты)

- **`humanoid_fr_sync`** (cron 3ч) — хардкод-список **~22 листов** из 6 файлов
  (`Fundraising Status_CEO Офис`, `Followers`, `List_Series_A` (20 листов),
  `Tatiana` xlsx, `Xasis` xlsx, `European SFO Targets+Xasis`).
- **`humanoid_fr_process_sheet`** на каждый лист: read → **MD5-хэш строки**
  (change-detection по `file_id::sheet::row_index`) → только изменённые строки →
  **GPT-4.1-mini `classify_and_extract`** (парсит грязную строку в структуру +
  нормализует enum'ы и имя: «Nvidia (Барсуков)» → company+contact) →
  **upsert в Supabase `humanoid_fr_companies`** (dedup по `company_name_normalized`,
  merge `source_sheets`+`row_hashes`) → лог `humanoid_fr_events`.
- **MCP** `search`/`dashboard`/`history` — Supabase RPC; `humanoid_fr_search` =
  **fuzzy-поиск** (pg_trgm) по имени + фильтры status/assignee/entity_type/deadline.

**Ключевое:** отдельной «ноды-критика» НЕТ. LLM работает на **ingest** (строка→структура),
на **запросе** — обычный fuzzy-поиск. «Финальный критик под контекст» — это **сам
чат-агент в Slack**: зовёт `humanoid_fr_search` и своим разумом выбирает из результатов.
Его преимущество = **ширина** (22 листа в одной чистой БД) + **LLM-нормализация на
ingest** + **рассуждение консумер-агента**.

## 3. Наш план — НЕ дублировать его ingest

У Виктора уже есть готовая, нормализованная, дедуплицированная таблица
`humanoid_fr_companies` (~660 active / 1415 всего) в Supabase `uqbcabewzyenidihbxln`.
У нас в раннере уже есть коннект в **тот же проект** (`telegram_source_database_url`).
→ читаем его таблицу **напрямую** и зеркалим в наш каталог. Один источник правды,
нормализованный его пайплайном, ноль дублирования.

| # | Решение |
|---|---|
| D1 | **Track 1 (recall, главный выигрыш):** зеркалим `humanoid_fr_companies` → `entity_catalog_staging`. Cron, read-only к его БД. |
| D2 | Доступ: роль `humanoid_reader` (TG) → проверить `GRANT SELECT` на `humanoid_fr_companies`; если нет — попросить Виктора дать read-роль ИЛИ REST-эндпоинт. |
| D3 | Перед каждым зеркалированием — **snapshot** нашего каталога (pg_dump) → откат одной командой. |
| D4 | После зеркалирования наш `entity_match_v2` видит его 660+ компаний → recall-фикс закрывает жалобу. |
| D5 | **Track 2 (disambiguation, опц.):** контекст-критик на шаге `canonicalize` встречи — повторяет «разум агента» в нашем не-чатовом пайплайне. За флагом, shadow-first. |
| D6 | Всё за флагами (`ENTITY_FR_MIRROR_ENABLED`, `ENTITY_CRITIC_ENABLED`/`_SHADOW`), default false. При off — текущий резолвер без изменений. |

## 4. Track 1 — зеркало FR-БД (делаем первым)

```
[cron, ops/mirror_fr_companies.py, за флагом ENTITY_FR_MIRROR_ENABLED]
  Supabase humanoid_fr_companies (read-only)
        │  SELECT company_name, company_name_normalized, entity_type,
        │         current_status, industry, contact_person, aliases?, source_sheets
        ▼
  snapshot нашего каталога (pg_dump entity_catalog_staging → /traces/catalog_backup_<ts>.sql)
        ▼
  upsert в entity_catalog_staging по name_normalised
        ├─ name = company_name (каноническая форма)
        ├─ is_org = (entity_type != angel_prospect)
        ├─ parent_org / aliases (contact_person, прежние формы)
        ├─ source_list = source_sheets[].sheet_name (провенанс)
        └─ attrs = {entity_type, current_status, industry}  (для критика)
```

- Маппинг его полей → наш `EntityCatalogStaging` (аддитивно добавить `source_list TEXT`,
  `attrs JSONB`, оба nullable; старые строки = NULL, vector-поиск не меняется).
- Идемпотентно по `name_normalised`; чужие листы НЕ перетирают наши вручную внесённые
  строки (merge, не replace).

## 5. Track 2 — контекст-критик (опционально, после Track 1)

Для каждого спорного упоминания на шаге `canonicalize`:
retrieval кандидатов (vector+lex по каталогу, теперь с его данными) → **один LLM-вызов**
с контекстом встречи (title, participants, окно транскрипта) + кандидаты с `attrs` →
`{canonical, source_list, confidence, reason}` | `none`. Применяем при `confidence≥порог`
и `≠current`; иначе текущий резолв. Лог в `entity_critic_decisions` (append-only).

Чистые/тестируемые юниты: `assemble_candidates`, `build_critic_prompt`,
`parse_critic_response`, `should_apply(decision, current, min_confidence)`.

## 6. Данные (аддитивно)

- `entity_catalog_staging` +`source_list TEXT` +`attrs JSONB` (nullable).
- `entity_critic_decisions` (новая, append-only) — только если делаем Track 2.
- Миграция `0041_entity_quality.py` (`down_revision=0040`).

## 7. Инварианты безопасности

- **I1.** Все флаги off → текущий `entity_match_v2`, ноль изменений.
- **I2.** Зеркало — read-only к чужой БД; пишем ТОЛЬКО в `entity_catalog_staging`, не в `counterparties`/`tasks`.
- **I3.** Перед каждым зеркалированием — snapshot каталога (откат `psql < backup`).
- **I4.** Критик: низкая confidence/none/ошибка → текущий резолв (имя всегда есть).
- **I5.** `ENTITY_CRITIC_SHADOW=true` → только лог, без подстановки.

## 8. Бэкап и откат

| Что | Бэкап | Откат |
|---|---|---|
| Спека/код | git-ветка + версия (`7d71e56` = rev1) | `git revert`/`checkout` |
| Каталог перед зеркалом | `pg_dump entity_catalog_staging` → `catalog_backup_<ts>.sql` | `psql < backup` |
| Применённые формы (Track 2) | `entity_critic_decisions` append-only | re-apply по логу |
| Поведение | env-флаги off + shadow | снять флаг, без передеплоя кода |

## 9. Тесты

- `test_mirror_fr_companies.py` — маппинг его полей → каталог; идемпотентность; merge-не-replace; snapshot вызывается перед записью (мок Supabase + мок pg_dump).
- `test_entity_critic.py` (Track 2) — assemble/prompt/parse/should_apply; shadow не подставляет.

## 10. Раскатка

- **E0** — миграция additive (поля каталога). Прод не меняется.
- **E1** — проверить доступ к `humanoid_fr_companies` (D2). Нет доступа → запрос Виктору.
- **E2** — `ENTITY_FR_MIRROR_ENABLED=true`: разовый зеркало-прогон (после snapshot), сверить размер каталога до/после, выборочно проверить имена из followers.
- **E3** — встреча проходит резолвер с обогащённым каталогом; сверить, что имена из жалобы Иры теперь корректны.
- **E4** (опц.) — Track 2 критик: shadow-неделя → включить.

## 11. Интеграция в пайплайн встреч (rev3 — prod)

**Точка встройки (rev5 — transcript-first):** в `_step_detailed_summary`
резолвер вызывается ДО построения detailed summary — на **сыром транскрипте**:
`improved_transcript = canonicalize_text(transcript, fr_map)`. Из улучшенного
транскрипта строится detailed → из detailed задачи → из detailed короткое
(всё с уже подменёнными именами). После старой `canonicalize_summary_text`
(team+counterparty) карта `fr_map` применяется к detailed ПОВТОРНО, чтобы FR-
формы побеждали ошибки старого резолвера («Amazon»→«Amazon.com»,
«Accenture Ventures»→имя человека) и вливается в `_detail_canon_map` для задач.

```
detailed_summary (LLM, canonical RU)
   └─ canonicalize_summary_text → applied {found: canonical}  (people + counterparty)
        └─ resolve_for_meeting(detailed_summary):                 ← НОВОЕ, за флагом
             fetch CRM dump (MCP, cached) → shard ≤30K + team roster
             → parallel maps + critic → decisions
             → build_replacements (confidence ≥ ENTITY_FR_MIN_CONFIDENCE)
        └─ APPLY: detailed_summary = canonicalize_text(text, replacements);
                  merge replacements в _detail_canon_map → задачи/короткое
        └─ SHADOW: НЕ меняем текст/карту; только пишем решения в БД
   → лог КАЖДОГО решения в entity_fr_decisions (для сверки/отката)
```

**Режимы (`app/services/fr_resolve_step.resolve_for_meeting`):**
- `ENTITY_FR_RESOLVER_ENABLED=false` → шаг не выполняется (ноль изменений, I1);
- `…ENABLED=true, …SHADOW=true` → считает + логирует, **текст не трогает**;
- `…ENABLED=true, …SHADOW=false` → применяет замены ≥ порога и вливает в карту.

**Данные:** `entity_fr_decisions` (id, source, source_id, mention, canonical,
source_list, confidence, applied, shadow, created_at) — append-only; миграция
`0041` additive (`down_revision=0040`).

**Инварианты:** best-effort (любой сбой резолвера НЕ ломает шаг — try/except,
саммари строится как раньше); reuse карты замен = consistency гарантирована;
люди-сотрудники из team_members, внешние контакты-люди — вне CRM (отдельно).

## 12. Три трека резолва (rev4)

Резолв делится по ИСТОЧНИКУ сущности, результаты помечаются `kind`:

| Трек | Что | Источник | Механизм |
|---|---|---|---|
| **company** | контрагенты-компании | lean-каталог CRM | шарды ≤30K + критик (Track 1) |
| **team** | участники команды | наша `team_members` | ростер-срез в той же map-reduce |
| **person** | контрагенты-люди | `next_action`/`last_update`/`communication_log` записей CRM | **агентный 2-й проход** |

**Агентный проход для людей** (`app/services/entity_people_fr.resolve_people`) —
дёшево и точно, как у бота Виктора (компании lean'ом, людей добираем точечно):

```
unresolved person-like mentions (canonical=None из Track 1/3)
   └─ LLM #1: для каждого — это человек? из какого орга/фонда? (по контексту встречи)
        → [(mention, org_guess)]
   └─ для каждого org_guess: humanoid_fr_search(org)  → ПОЛНАЯ запись с comm_log
        (точечно: только нужные орги, ≤ ENTITY_FR_PEOPLE_MAX_ORGS)
   └─ LLM #2: из прозы записей достать каноническое ПОЛНОЕ имя
        → [Decision(mention→canonical, source, confidence, kind=person)]
```

Подтверждено зондом: «Samer Zawadeih» лежит в `communication_log` записи
*Tawazun Strategic Development Fund* (запись 02/04) — lean его режет, агентный
проход читает полную запись по орг-контексту и достаёт.

**За флагом** `ENTITY_FR_PEOPLE_ENABLED` (default false). Best-effort: сбой
2-го прохода НЕ влияет на Track 1/3. Параметры: `ENTITY_FR_PEOPLE_MAX_ORGS`
(сколько орг-записей тянуть, default 6).

## 13. Открытые вопросы

- Доступ роли `humanoid_reader` к `humanoid_fr_companies` (проверить SELECT; иначе грант/REST у Виктора).
- Нужен ли вообще Track 2, если Track 1 (его чистые данные в нашем резолвере) уже закрывает recall — решаем после E3 по факту жалоб.
- Частота зеркала: его sync 3ч → нам хватит раз в 6–12ч (имена меняются медленно).
- Не дублируем ли мы Виктора по смыслу — может, начальнице достаточно его бота, а нам — только доставка имён в саммари/задачи (тогда Track 1 и хватит).
