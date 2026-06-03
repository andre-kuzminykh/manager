# SPEC v0.1 — Bidirectional Sheet ↔ DB (Google Sheet как интерфейс, БД — истина)

> **Дата:** 2026-06-03 · **Эпик:** `FR-SS-*` (Sheet Sync bridge)
> **Связано:** [SPEC_STATUS_TRACKER_v0.2.md](./SPEC_STATUS_TRACKER_v0.2.md) (лог/откат S0),
> [docs/SPEC_TASK_VECTOR_v0.1.md](./docs/SPEC_TASK_VECTOR_v0.1.md), [AUDIT.md](./AUDIT.md).
> **Флаг:** `SHEET_SYNC_BRIDGE_ENABLED` (default false).

## 0. Решение об архитектуре (почему мост на System B)

В репе две подсистемы (см. карту):
- **System A** (`app/sync/sheets.py`) — пишет в `tasks`, но идентичность строки = **позиционный номер**
  (`Task.google_sheets_row_id` / `GoogleSheetsSync.row_id`). Человек удалил/вставил/отсортировал строку →
  номера съехали, маппинг разрушен. Не детектит удаления, не создаёт из пустой строки.
- **System B** (`app/sheet_sync/`, `gs_*`) — идентичность через **DeveloperMetadata `gs_row_uuid`**
  (переживает любые перестановки), append-only `gs_record_states` (история+откат), детект удалений по
  отсутствию, создание новых строк (uuid+writeback), снапшоты+хеши для cell-diff. Но пишет только в `gs_*`.

Поскольку интерфейс-редактор по определению двигает/удаляет/добавляет строки, **позиционная идентичность
System A непригодна.** Решение: **новый мост `app/sheet_sync/bridge.py`**, который соединяет надёжную
машинерию System B (идентичность/история/дифф/детект) с реальными `tasks` и логом S0. System A для задач
выводится из эксплуатации (миграция §12).

## 1. Принципы и инварианты

- **I1. БД — единая истина.** Структура, id, валидность, связи — из БД. Sheet — проекция + интерфейс правки.
- **I2. Любое изменение, в любую сторону, логируется в `task_status_events` (S0)** и откатываемо
  (`ops/rollback_task_status.py`). Sheet-правки получают `source="sheet"`.
- **I3. Идемпотентность.** Нет различий → нет записей (ни в БД, ни в Sheet).
- **I4. Fail-safe.** Любая ошибка строки изолирована (помечается, не валит цикл). Пустое/ошибочное
  чтение Sheet → **abort** (никогда не трактуем как «удалить всё»).
- **I5. Минимум записей в Sheet.** Пишем только отличающиеся ячейки, батчем.

## 2. Идентичность строки (FR-SS-ID)

- **FR-SS-ID-1:** каждая строка несёт `gs_row_uuid` в **DeveloperMetadata** (механизм System B,
  `sheets_client.stamp_row_uuids`). Это первичная идентичность — переживает сортировку/вставку/удаление.
- **FR-SS-ID-2:** мост-таблица `gs_task_row_mapping` (есть в System B как `GsTaskRowMapping`, но не
  заполняется — оживляем): `internal_row_uuid ↔ task_id ↔ last_row_number ↔ last_task_signature ↔ last_payload_hash`.
- **FR-SS-ID-3:** видимая read-only колонка `Task ID` (для человека) — НЕ источник идентичности (человек
  может стереть). При расхождении видимого id и DeveloperMetadata — истина у метаданных.
- **FR-SS-ID-4:** строка **без** `gs_row_uuid` = новая (заведена человеком) → ветка create (§5.3).

## 3. Схема колонок (канон — TASK_HEADERS + Task ID)

Берём `app/sheet_sync/config.py:TASK_HEADERS` (16 колонок) + добавляем read-only `Task ID` (или прячем в
metadata). E = editable (тянем из Sheet в БД), R = read-only/system (только пишем из БД).

| # | Колонка | Task-поле | dir |
|---|---|---|---|
| 0 | Task ID | `id` | R |
| 1 | Task title | `title` (required) | E |
| 2 | Description | `description` | E |
| 3 | Responsible | `owner_*` (резолв в id) | E |
| 4 | Status | `status` | E |
| 5 | Priority | `priority` | E |
| 6 | Category | `extra.direction` | E |
| 7–12 | Start/Deadline/Completion date+time | `start_*`/`due_*`/`completed_*` | E |
| 13 | Comments | comment-only событие (§9) | E |
| 14 | Added at | `created_at` | R |
| 15 | Источник | `source_kind` | R |
| 16 | Ссылка | `source_permalink` | R |
| 17 | sync_error | диагностика строки (§10) | R |

## 4. Цикл reconcile (`ops/sheet_sync_bridge.py --loop`, крон 10 мин)

Порядок строгий: **сперва PULL (правки человека), потом PUSH (истина БД).** Это даёт «sheet-wins при
одновременной правке» (§7) бесплатно.

```
tick:
  rows = read_all_with_metadata()         # значения + gs_row_uuid + row_number
  if rows is ERROR or (rows == [] and mapping not empty):  abort(I4)
  snapshot_prev = load GsSheetSnapshot     # последнее, что мы знали о Sheet
  # ---- PULL Sheet → DB ----
  for row in rows:
      if no uuid:           create_task_from_row(row)        # §5.3
      else if differs from DB on editable field: apply_edit  # §5.1
  detect_deletes(mapping, rows)            # uuid был, строки нет → soft-delete  §5.2
  # ---- PUSH DB → Sheet ----
  diff = cells where DB_task != row_value (после pull)        # §6
  values.batchUpdate(diff)                 # один вызов, только изменённые ячейки
  append rows for live DB tasks not yet in sheet (chat/meeting-origin)
  tombstone rows for DB-soft-deleted tasks not deleted via sheet
  save GsSheetSnapshot(new normalized state, row_hashes)
```

## 5. PULL — Sheet → DB

### 5.1 Правка поля (FR-SS-EDIT)
- **FR-SS-EDIT-1:** для строки с uuid → задача по `gs_task_row_mapping`. Для каждого E-поля, где значение
  Sheet ≠ БД **и** изменилось с прошлого снапшота (по `payload_hash`) → апдейт поля задачи.
- **FR-SS-EDIT-2:** `status` → через `TransitionService().apply(actor="sheet", reason="sheet_edit")`
  (валидация перехода + history). Невалидный переход → не применять, `sync_error`.
- **FR-SS-EDIT-3:** `Responsible` → резолв в `owner_user_id` (TeamMember/Employee, как `_resolve_owner_back_to_id`).
  Не резолвится → `sync_error`, поле не меняем.
- **FR-SS-EDIT-4:** каждое применённое изменение → `record_status_event(source="sheet", field, from→to, actor="sheet")` (S0).

### 5.2 Удаление строки (FR-SS-DEL)
- **FR-SS-DEL-1:** uuid есть в `gs_task_row_mapping`, но строки с этим uuid **нет** в текущем чтении →
  человек удалил строку → `task.deleted_at = now()` + `record_status_event(source="sheet", field="deleted")`.
- **FR-SS-DEL-2 (защита):** срабатывает ТОЛЬКО если чтение Sheet валидно и непусто (I4); и только для
  uuid, подтверждённо замапленных. Массовое исчезновение (>N% строк за тик) → abort + alert, НЕ удаляем.
- **FR-SS-DEL-3:** альтернативный явный сигнал — статус `Cancelled` в колонке Status → тоже soft-delete
  (без физического удаления строки). Оба пути ведут в один `deleted_at`.

### 5.3 Новая строка → новая задача (FR-SS-NEW)
- **FR-SS-NEW-1:** строка без uuid и с непустым `title` → создать `Task(source_kind="sheet")`:
  status (default `todo` если пусто), owner (резолв), due (парс), priority, direction из Category.
- **FR-SS-NEW-2:** проставить новый `task_id` в колонку Task ID + застампить `gs_row_uuid` (writeback) +
  завести `gs_task_row_mapping`. Событие `record_status_event(source="sheet", field="created", to=...)`.
- **FR-SS-NEW-3:** пустой `title` или битые поля → не создаём, пишем `sync_error`, строку не трогаем.

## 6. PUSH — DB → Sheet (только различия)

- **FR-SS-PUSH-1:** для каждой живой задачи сравнить её поля с последним снапшотом строки; собрать список
  **изменившихся ячеек**; записать одним `spreadsheets.values.batchUpdate` (не построчно — см. §11).
- **FR-SS-PUSH-2:** задача, созданная в БД (чат/встреча) и не имеющая строки → append + stamp uuid + mapping.
- **FR-SS-PUSH-3:** задача `deleted_at` (удалена не через Sheet) → строку в tombstone: Status=`Cancelled`
  (или удалить строку — конфиг `SHEET_SYNC_TOMBSTONE_MODE`).
- **FR-SS-PUSH-4:** R-поля (id/source/created_at) пишутся всегда из БД (человек не может их менять — при
  расхождении перезаписываем).

## 7. Разрешение конфликтов (FR-SS-CONF)

- **FR-SS-CONF-1 (sheet-wins на одновременной правке):** PULL раньше PUSH → правка человека уходит в БД,
  затем PUSH видит совпадение → не перезатирает.
- **FR-SS-CONF-2 (anti-lost-update):** перед применением Sheet-правки сравнить `task.updated_at` с
  `gs_sheet_snapshot.captured_at`. Если БД менялась **позже** последнего снапшота (т.е. был апдейт из
  чата/встречи, которого человек не видел) → **БД побеждает** по этому полю (PUSH перезапишет ячейку,
  Sheet-значение НЕ применяем), событие `sheet_edit_skipped_stale`. Per-field.
- **FR-SS-CONF-3:** изменение определяется по `payload_hash` поля vs снапшот — «человек реально менял
  ячейку», а не «значение просто отличается от БД из-за дрейфа».

## 8. Маппинг статусов/приоритетов (FR-SS-MAP)

- Task enum = `{backlog, todo, in_progress, done}`. Sheet-дисплеи (`STATUS_DISPLAY_BY_KEY`) включают
  `Blocked`/`Cancelled`, которых в enum нет.
- **FR-SS-MAP-1:** `To Do/In Progress/Done/Backlog` ↔ enum (как `STATUS_NORMALIZED`).
- **FR-SS-MAP-2:** `Cancelled` (из Sheet) → soft-delete (`deleted_at`) + событие; обратно: `deleted_at` → `Cancelled`.
- **FR-SS-MAP-3:** `Blocked` (из Sheet) → нет enum-аналога: оставляем текущий статус + comment-событие
  «blocked» (или вводим enum `blocked` отдельной миграцией — открытый вопрос §13).
- **FR-SS-MAP-4:** priority `urgent` коллапсит в `High` при показе (известная потеря round-trip);
  не понижаем `urgent`→`high` в БД, если ячейка не менялась человеком (anti-stale, §7).

## 9. Интеграция с S0 (лог + откат)

- **FR-SS-S0-1:** каждое изменение БД из моста → `record_status_event(source="sheet", ...)`:
  правки → field/from→to; удаление → `field="deleted"`; создание → `field="created"`; комментарий →
  `field="comment"`.
- **FR-SS-S0-2:** колонка `Comments` (не пустая, изменилась) → comment-only событие (S0), задачу не меняет.
- **FR-SS-S0-3:** откат (`ops/rollback_task_status.py`) восстанавливает значение в БД; ближайший PUSH
  отразит это в Sheet. Так «откат из мастер-таблицы» = одна команда по event_id (виден в `--list`).
- Внутренний `gs_record_states` остаётся механизмом System B (идемпотентность/дифф); **пользовательский
  лог отката — единый `task_status_events`**.

## 10. Безопасность и диагностика

- **FR-SS-SAFE-1:** пустое/ошибочное чтение Sheet → abort (I4).
- **FR-SS-SAFE-2:** массовое удаление (>`SHEET_SYNC_MAX_DELETE_PCT`, default 20%) за тик → abort + alert.
- **FR-SS-SAFE-3:** битая строка → `sync_error` ячейка, строка пропускается, цикл продолжается.
- **FR-SS-SAFE-4:** `--dry-run` — лог diff без записи (БД и Sheet).
- **FR-SS-SAFE-5:** один writer на таблицу (advisory lock / single runner), чтобы PULL и PUSH не гонялись.

## 11. Квоты / батчинг

- **FR-SS-Q-1:** все изменённые ячейки PUSH — **один `spreadsheets.values.batchUpdate`** (сейчас System A
  делает per-task `values.update` — неэффективно; строим batch).
- **FR-SS-Q-2:** структура/метаданные/dropdown — один `batchUpdate` (механизм System B).
- **FR-SS-Q-3:** throttle под 60 зап/мин + tenacity-retry на `HttpError` (как в System A).

## 12. Миграция с System A

- **FR-SS-MIG-1:** одноразовый `ops/sheet_sync_bridge.py --migrate` — по текущему листу System A
  (`tab=Main`, task_id в колонке A) застампить `gs_row_uuid` каждой строки, завести `gs_task_row_mapping`
  по видимому task_id, снять первый снапшот. После этого мост ведёт лист по DeveloperMetadata, позиционный
  `Task.google_sheets_row_id`/`GoogleSheetsSync` больше не источник истины (оставляем, не используем).
- **FR-SS-MIG-2:** System A `SheetsSyncService.sync`/`SheetsPullService.pull` отключаются (флаг), чтобы не
  гонять два писателя по одному листу.

## 13. Открытые вопросы

- `Blocked` — вводить enum `blocked` (миграция + транзишены) или comment-маппинг? (По умолчанию comment.)
- Tombstone: физически удалять строку DB-удалённой задачи или красить `Cancelled`? (`SHEET_SYNC_TOMBSTONE_MODE`).
- Видимая колонка `Task ID`: показывать (удобно человеку) или прятать (чище)? (По умолчанию показывать, read-only.)

## 14. Тесты (traceability)

- `test_sheet_bridge_pull_edit.py` — правка поля → апдейт БД + S0 событие; невалидный статус → sync_error;
  anti-stale (БД новее → Sheet-правка не применяется).
- `test_sheet_bridge_delete.py` — uuid пропал → soft-delete + событие; защита от массового удаления (abort);
  `Cancelled` → soft-delete.
- `test_sheet_bridge_create.py` — строка без uuid+title → новая Task + writeback id/uuid + событие; пустой
  title → sync_error, не создаём.
- `test_sheet_bridge_push_diff.py` — пишем только изменённые ячейки; нет различий → нет записей (идемпотентность).
- `test_sheet_bridge_conflict.py` — sheet-wins на одновременной правке; БД-новее → db-wins.
- переиспользуют фейки `app/sheet_sync/sheets_client` + sqlite (как S0-тесты).

## 15. План выкатки

| Этап | Что | Гейт |
|---|---|---|
| B0 | мост-модель `gs_task_row_mapping` живая + read_all_with_metadata + снапшот | юнит-тесты identity/snapshot |
| B1 | PULL (edit/delete/create) + S0 события + sync_error | тесты §14 pull/delete/create зелёные |
| B2 | PUSH cell-diff (batchUpdate) + conflict (anti-stale) | тесты push/conflict; shadow-лист |
| B3 | миграция с System A + крон 10 мин + отключить System A | сутки на проде, расхождений нет |
