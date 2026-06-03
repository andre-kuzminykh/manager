# AUDIT — спеки ↔ код (2026-06-03)

> FR-by-FR сверка каждой фиче-спеки с кодом (`app/`, `ops/`, `tests/`, `alembic/`).
> Метод: греп FR-ID + поведение/функции/флаги, проверка `app/config.py` и `.env.example`,
> существование файлов-ссылок, корректность claim статуса. Источник истины — **код**.
> Ниже на каждую спеку: вердикт · флаги/env · битые ссылки · счётчики · **расхождения**
> (строки IMPLEMENTED опущены ради читаемости — указаны счётчиком).

Сквозная находка: **`.env.example` неполон для всех фич** — задокументирован только
`MEETING_WEBHOOK_URL`. Все прочие флаги есть в `app/config.py`, но не в `.env.example`.

---

## 1. Note Taker — `SPEC_NOTE_TAKER_v0.1.md` ⚠️

**Статус:** «MVP уже работает» — в основном **верно** (33/43 FR). FR-NT-* ID в коде нет (код — `FR-CR-05-*`).
**Счётчики:** IMPLEMENTED 33 · PARTIAL 3 · MISSING 4 (3 из них спека сама помечает TODO) · DIVERGES 3.
**Флаги:** есть в `config.py`; `ZOOM_POLL_BATCH_SIZE` дефолт **10** (не 50), `FIREFLIES_POLL_BATCH_SIZE` **20** (не 50).

**Расхождения:**
| FR | требует | реальность | вердикт |
|---|---|---|---|
| FR-NT-9.6 | DB-view `meeting_summaries_published` + роль `zoom_colleague` (помечено «работает») | нет ни миграции, ни кода — только текст спеки | **MISSING** |
| FR-NT-2.3 | переиспользовать готовый транскрипт Fireflies | `fetch_transcript_text` есть, но не вызывается — всегда Whisper (лишняя стоимость) | **DIVERGES** |
| FR-NT-8.4 | dedup SequenceMatcher ≥ **0.85** | порог **0.70** (`task_dedup.py:268`) | **DIVERGES** |
| FR-NT-1.2/1.4 | batch 50 | дефолты 10 / 20 | DIVERGES (низкий риск) |
| FR-NT-3.1 | L1 file-size 0<N<MAX | верхний кап есть, отдельной нижней проверки нет | PARTIAL |
| FR-NT-3.4 | retry на пустом detailed_summary | ошибка без явного ретрая | PARTIAL |
| FR-NT-1.5/1.6/1.7 | manual upload / dictation / GMeet | не построено (спека помечает TODO) | MISSING (ок) |

Минор: §12.5 обещает `docs/prompts/*.md`, но промпты — Python-константы в `app/fireflies/prompts.py`.

---

## 2. Meeting Agenda — `SPEC_MEETING_AGENDA_v0.1.md` ⚠️

**Статус:** реализовано и в `app/main.py`, 64 теста (не 21). Спека устарела (13 мая). FR-MA-* в коде нет.
**Счётчики:** IMPLEMENTED ~26 · PARTIAL 2 · DIVERGES 1.

**Расхождения:**
| FR | требует | реальность | вердикт |
|---|---|---|---|
| FR-MA-3.1 | open_tasks фильтр по source_kind/status/deleted | код добавляет недокументированный фильтр `extra.direction ∈ DIRECTIONS_IMPORTANT` (FR-CR-05-192aa, `service.py:333`) — задачи без direction отбрасываются; **ломает собственный тест спеки** `test_open_tasks_for_recordings_filters_status_and_orders_by_priority` | **DIVERGES** |
| §16.3 | `AGENDA_MIN_PRIOR_MEETINGS=1` | дефолт **2** (`config.py:250`) | DIVERGES |
| §12.3 | `limit=30` | дефолт **100** | DIVERGES |
| FR-MA-3.2 | sort priority/due/created/id | `created_at` не в sort-key, по id | PARTIAL |

`AGENDA_*` (9 ключей) есть в `config.py`, в `.env.example` — нет.

---

## 3. Meeting Webhook — `SPEC_MEETING_WEBHOOK_v0.1.md` ✅

Авторская спека от 2026-06-03, **совпадает с кодом**. Все контрактные требования IMPLEMENTED,
тесты зелёные (`test_meeting_webhook.py` 10 + `test_summary_has_body.py` 7). `MEETING_WEBHOOK_URL`
есть и в `config.py`, и в `.env.example`. Открытые TODO (webhook-before-commit B2, кастомный
раннер B3, confidence/hallucination-гейты B4/B5) спека сама помечает ⚠️ — не расхождение.

---

## 4. Entity Consistency — `SPEC_ENTITY_CONSISTENCY_v0.1.md` ✅

Авторская от 2026-06-03, **совпадает с кодом**, симметрично Zoom + Fireflies, тесты зелёные.
- detailed канонизируется → карта на `_zm/_ff_detail_canon_map` (`zoom/pipeline.py:721`, `ff:1376`)
- short строится из detailed без повторной канонизации (FR-CR-05-241)
- задачи форсятся к карте detailed (`zoom:1483`, `ff:1868`)
- roster-guard people (`summary_canonicalize.py:117`)
- тесты `test_task_summary_consistency.py` (5) + `test_people_roster_guard.py` (4) — зелёные.
§6 (extract из канонического detailed; схлопнуть 2 LLM-прохода) — future work, не расхождение.

---

## 5. Counterparty Briefs — `SPEC_COUNTERPARTY_BRIEFS_v0.1.md` ✅

Код соответствует **v0.2** дизайну (event-trigger + two-stage research + grouped-DM). ~58/70 IMPLEMENTED.
**Расхождения / шум:**
| Что | Реальность |
|---|---|
| Версия | конфликт в заголовке: тело «v0.2 revised», футер «v0.1» |
| §12.1 миграция `0029` | реально **`0030_counterparty_briefs.py`** |
| §12.3 имена сервисов (v0.1) | код использует v0.2-сплит (`extract_event_counterparties`+`extract_beneficiaries`, `lookup_org`, `research_org`+`research_person`, `build_org/person_doc_body`) — не дефект |
| FR-CB-1.2 lookahead | спека сама пишет 7 и 14; код = **7** (operator-pinned) |
| FR-CB-3.1 person-via-`counterparty_mentions` | нет отдельного теста; `lookup.py` отдаёт только `lookup_org` (person, возможно, в beneficiary-research) — PARTIAL |
| CLI `--re-render-docs/--seed-existing/--list/--lookback` | есть, но без авто-тестов — PARTIAL |

`COUNTERPARTY_BRIEFS_*` есть в `config.py`, в `.env.example` — нет.

---

## 6. Task Tracker — `SPEC_TASK_TRACKER_v0.1.md` ⚠️ (СТАРАЯ)

**Статус:** спека от 2026-05-08 СИЛЬНО отстала. **Недооценивает** (помечено TODO, но реализовано):
Slack-ingest, утренний/вечерний дайджесты, deadline-reminders, Google Tasks 2-way (push+pull),
подписки. FR-TT-* ID в коде нет.
**Счётчики (≈47):** IMPLEMENTED ~22 · PARTIAL ~8 · DIVERGES ~9 · MISSING ~5.

**Топ-расхождения:**
| FR | требует | реальность |
|---|---|---|
| FR-TT-7.1 / NFR-TT-U.1 | 7 русских кнопок (Принять/Делегировать/Отложить/Изменить/Закрыть/Подписаться/Refresh) | 6 английских (Accept/Edit/Delete/Mark done/Subscribe/Unsubscribe), **без Delegate/Postpone/Refresh** (`keyboards.py:75`) |
| FR-TT-7.4 | flow делегирования | отсутствует |
| FR-TT-2.1 | intent {task/chitchat/question/status_update} | код: `create_task/update_task/no_action` (`schemas/intent.py:9`) |
| FR-TT-7.3/13.1 | таблица `task_status_changes` | реально `task_status_history` (`models/task.py:226`) |
| FR-TT-10.* | `recurring_task_rules` + крон | поля на `Task` (`is_recurring/recurring_weekdays`), без спавн-цикла |
| FR-TT-8.4 | tz через `TIMEZONE` | только `SHEET_SYNC_TIMEZONE` |
| §12.5 | OpenAI GPT-5.4 | код провайдер-агностик, дефолт **Anthropic** `claude-sonnet-4-6` |
| §12.2 | `recurring_scheduler.py`, `deadline_reminders.py`, `google_tasks_sync.py`, `app/email_ingest/` | **не существуют** (логика в других модулях / email-ingest нет вовсе) |

**Вывод:** спеку надо переписать под реальность (v0.2), а не код под спеку.

---

## 7. Task Extractor — `SPEC_TASK_EXTRACTOR_v0.1.md` ⚠️ (forward-looking)

**Статус:** унаследованный TG-пайплайн (по `FR-CR-*`) есть; **всё net-new из спеки — не построено.**
**Счётчики:** IMPLEMENTED ~14 · PARTIAL ~22 · MISSING ~16 · DIVERGES ~8.

**Не построено (MISSING):** Favorites целиком (`task_favorites`, ⭐, `/favorites`), chat-subscription
(`tg_chats_subscribed`, `/setup_chat`, `/disable_chat`, `my_chat_member`-онбординг), статус `blocked`
+ `block_reason`, `/audit <id>`. Файлы `favorites.py`, `chat_setup.py`, `status_service.py`,
`models/task_favorite.py`, `tg_chat_subscribed.py` — отсутствуют.

**DIVERGES:** status-enum = `backlog/todo/in_progress/done` (без `blocked`); кнопки иные;
`my_chat_member` не в `allowed_updates`; дефолт-дедлайн в ядре 23:59 (FR-CR-05-210), 18:00 только
в TG-ingest пути; аудит-таблица `task_status_history`, не `task_status_changes`.

---

## 8. Task Vector — `docs/SPEC_TASK_VECTOR_v0.1.md` ✅ (точная)

**Статус claim «P5 deployed read-only, P6 writes off» — ТОЧНЫЙ.** Оба флага дефолт `False`
(`config.py:403,410`), код реально гейтит: `build_task_executors(writes_enabled=False)` отдаёт
только read-инструменты (`task_tools.py:463`), responder подключает writes лишь при
`task_vector_writes_enabled` (`responder.py:919`).
**Счётчики (≈31):** IMPLEMENTED ~28 · PARTIAL 2 · MISSING-by-design 1 (FR-TV-013, спека сама помечает).

**Ключевое:** **P6 (апдейт статуса/срока/овнера) УЖЕ написан** — `update_task_status/due/owner`
(`task_tools.py:337-453`): валидация, history, sync в Sheets/Tasks/карточки, undo. Просто за флагом.

**Что мешает включить P6:**
| Что | Статус |
|---|---|
| `ops/gen_task_vector_eval.py` (синт-эвал для калибровки τ/δ, пререквизит NFR-TV-004) | **отсутствует** |
| MCP-сервер: `undo_last_task_status` + токен-аутентификация | TODO (в docstring) |
| `.env.example`: TASK_VECTOR_* / TASK_MCP_* | отсутствуют |

Тесты сильные: `test_task_vector_tools.py` (~20), `test_task_vector_indexing.py`, `test_task_mcp_server.py`.

---

## 9. CEO Brain Bot — `SPEC_CEO_BRAIN_BOT_v0.1.md` ⚠️ (код впереди спеки)

**Статус:** построено БОЛЬШЕ v0.1 — responder прошёл ~25 operator-pinned итераций. Cat 1,2,4,5 — GREEN;
Cat 3 — в основном GREEN; **Cat 6 (Operations) — не построена.**
**Счётчики (≈74):** IMPLEMENTED ~52 · PARTIAL 6 · MISSING 7 · DIVERGES 3.

**Расхождения:**
| FR | требует | реальность |
|---|---|---|
| FR-CB2-6.1/6.2/6.3/6.4 | CLI export/backfill, health, Prometheus | 4 модуля **отсутствуют** (`ops/brain_archive_export.py`, `ops/brain_backfill.py`, `ceo_brain/health.py`, `metrics.py`); тесты xfail |
| NFR-CB2-C.2 | per-run cap **$5** | дефолт **$1.0** (`config.py:124`) |
| FR-CB2-3.8 | streaming chat_update | заброшено в пользу non-streaming (3.22) + per-MCP/direct-HTTP (3.30/3.31) |
| FR-CB2-3.9 | блок «Sources» | убран (3.29), вместо него инлайн-ссылки |
| (вне спеки) | — | код несёт **недокументированные** FR-CB2-3.27/3.32–3.39, 4.6/4.7 (Jira/Rovo) — спека отстаёт |
| NFR-CB2-R.2 / A.1 | reconnect без потерь / ротация JSONL | тесты xfail / логика ротации не построена |

Все `CEO_BRAIN_*` + `MCP_SERVERS` есть в `config.py`, в `.env.example` — нет.

---

## 10. Status Tracker — `SPEC_STATUS_TRACKER_v0.1.md` ⬜ SPEC-ONLY

**Кода НОЛЬ.** Греп по `task_status_events`, `status_tracker`, `person_aliases`, `THRESH_MATCH`,
`unmatched_candidates` → 0 совпадений. Нет таблицы `task_status_events`, нет пайплайна
(ingest→extract→identity→matcher→event-log→projector), нет `person_aliases`. Решения D1–D8 и все
секции — **MISSING (8/8)**. Спека корректно помечена как бриф будущей работы.

**⚠️ Пересечение с Task Vector:** обе фичи про «обновлять задачи». Status Tracker предлагает СВОЙ
вектор-матчер (cosine quote↔task, `text-embedding-3-small`, THRESH 0.72) и СВОЙ резолв людей
(`person_aliases`) — а Task Vector **уже имеет** индекс задач (`entity_embeddings`,
`text-embedding-3-large`), резолв команды и write-path (`sync/`). **Рекомендация:** строить Status
Tracker как ВТОРОЙ ВХОД поверх движка Task Vector (matcher + people + write-path переиспользовать),
добавив только meeting-extract + append-only event-log + авто-apply. Не строить второй вектор-индекс.

---

## Сводный список действий (по приоритету)

1. **`.env.example`** — добавить все флаги (CEO Brain, Counterparty Briefs, Task Vector, Agenda,
   Note Taker, Task Tracker). Фикс №1, бьёт по всем фичам.
2. **Task Vector P6** — написать `ops/gen_task_vector_eval.py`, откалибровать τ/δ, включить
   `TASK_VECTOR_WRITES_ENABLED`. Это и есть «обновлять задачи» — почти готово.
3. **Meeting Agenda FR-MA-3.1** — решить: документировать direction-фильтр в спеке ИЛИ убрать из кода
   (он ломает собственный тест спеки).
4. **CEO Brain Cat 6** — либо построить (export/backfill/health/metrics), либо понизить до planned;
   per-run cap привести к одному значению ($1 vs $5).
5. **Переписать `SPEC_TASK_TRACKER` и `SPEC_TASK_EXTRACTOR`** под реальность (снять ложные TODO,
   поправить кнопки/intent/таблицы/recurring) ИЛИ пометить их design-only и сослать на `FR-CR-*`.
6. **Status Tracker** — переписать как надстройку над Task Vector (убрать дубль матчера/людей/записи).
7. **Note Taker:** решить по `meeting_summaries_published`-витрине (построить или убрать claim),
   по Fireflies-транскрипту (подключить `fetch_transcript_text` — экономия Whisper), по dedup-порогу
   (0.70 vs 0.85 — выровнять спеку/код).
