# PRD — Humanoid CEO Brain (master)

> **Тип:** мастер-PRD = единая точка входа. Сам по себе он НЕ содержит требований —
> он индексирует **версионные фиче-спеки** и фиксирует их **соответствие коду**.
> **Дата сборки:** 2026-06-03 · **Ветка:** `claude/slack-bot-task-extraction-9eGSC`
> **Метод аудита:** FR-by-FR сверка каждой спеки с кодом (`app/`, `ops/`, `tests/`, `alembic/`),
> 2026-06-03. Детали — в [`AUDIT.md`](./AUDIT.md).

---

## 0. Как читать

```
PRD.md (этот файл) ──► фиче-спека (SPEC_*_v0.1.md) ──► код (app/…, FR-комментарии)
```

### Легенда статуса (рантайм)
| Бейдж | Значение |
|---|---|
| 🟢 **LIVE** | Поднимается в `app/main.py` без флага. |
| 🟡 **FLAG** | Реализовано, активируется env-флагом (в проде включено оператором). |
| 🔵 **JOB** | Запускается отдельным процессом `ops/*` (раннеры встреч и т.п.). |
| 🟠 **PARTIAL** | Реализовано частично / часть фич спеки не построена. |
| ⚪ **SPEC-ONLY** | Только бриф, в коде отсутствует. |

### Легенда аудита (спека ↔ код)
| Бейдж | Значение |
|---|---|
| ✅ | Спека совпадает с кодом. |
| ⚠️ | Есть расхождения (см. [`AUDIT.md`](./AUDIT.md)). |
| ⬜ | В коде не реализовано. |

---

## 1. Продукт

**CEO Brain** — «второй мозг» руководителя: монолитный сервис (Python + Postgres),
который слушает рабочие каналы (Slack, Telegram, Zoom, Fireflies), превращает разговоры
и встречи в **задачи**, **саммари** и **брифы**, ведёт их жизненный цикл, зеркалит всё в
Google (Sheets / Tasks / Docs / Calendar) и отвечает на вопросы в чате с доступом к реальным
данным. Один процесс (`app/main.py:run`) поднимает Slack-бот + кросс-канальную рассылку +
демоны Agenda и Counterparty Briefs + листенер CEO Brain. Тяжёлые пайплайны встреч и ingest —
отдельными процессами `ops/*`.

Пользователь — **Артем, CEO & Founder, Humanoid.**

---

## 2. Индекс фич (мастер-таблица)

| Фича | Каноничный FR-префикс¹ | Спека | Рантайм | Аудит² |
|---|---|---|---|---|
| **Note Taker** — встречи → саммари/задачи | `FR-CR-05-*` | [SPEC_NOTE_TAKER_v0.1.md](./SPEC_NOTE_TAKER_v0.1.md) | 🟢🔵 | ⚠️ |
| **Meeting Agenda** — преднастрочная агенда | `FR-CR-05-165` | [SPEC_MEETING_AGENDA_v0.1.md](./SPEC_MEETING_AGENDA_v0.1.md) | 🟢 | ⚠️ |
| **Meeting Webhook** — доставка в n8n | `FR-CR-05-160` | [SPEC_MEETING_WEBHOOK_v0.1.md](./SPEC_MEETING_WEBHOOK_v0.1.md) | 🔵 | ✅ |
| **Entity Consistency** — одна форма сущности | `FR-CR-05-242/-241` | [SPEC_ENTITY_CONSISTENCY_v0.1.md](./SPEC_ENTITY_CONSISTENCY_v0.1.md) | 🟢 | ✅ |
| **Counterparty Briefs** — брифы к встречам | `FR-CR-05-168` (`FR-CB-*`) | [SPEC_COUNTERPARTY_BRIEFS_v0.1.md](./SPEC_COUNTERPARTY_BRIEFS_v0.1.md) | 🟡 | ✅ |
| **Task Tracker** — жизненный цикл задач | `FR-CR-*` | [SPEC_TASK_TRACKER_v0.1.md](./SPEC_TASK_TRACKER_v0.1.md) | 🟢 | ⚠️ |
| **Task Extractor** — задачи из TG/Slack | `FR-CR-*` | [SPEC_TASK_EXTRACTOR_v0.1.md](./SPEC_TASK_EXTRACTOR_v0.1.md) | 🟠 | ⚠️ |
| **Task Vector** — поиск/Q&A/апдейт задач NL | `FR-TV-*` | [docs/SPEC_TASK_VECTOR_v0.1.md](./docs/SPEC_TASK_VECTOR_v0.1.md) | 🟡 | ✅ |
| **CEO Brain Bot** — чат-агент | `FR-CB2-*` | [SPEC_CEO_BRAIN_BOT_v0.1.md](./SPEC_CEO_BRAIN_BOT_v0.1.md) | 🟡 | ⚠️ |
| **Status Tracker** — встречи → статус в Sheet | _(planned)_ | [SPEC_STATUS_TRACKER_v0.1.md](./SPEC_STATUS_TRACKER_v0.1.md) | ⚪ | ⬜ |

¹ **Важно:** в коде требования трассируются префиксами `FR-CR-05-*`, `FR-CB2-*`, `FR-TV-*`,
`FR-CR-05-168`. «Дизайнерские» ID из спек (`FR-NT-*`, `FR-TT-*`, `FR-TX-*`, `FR-MA-*`) в коде
**не встречаются** — это спецификационная нумерация, не трассировка. Канон для PRD — реальные
префиксы выше.

² Полные FR-by-FR таблицы и список расхождений — [`AUDIT.md`](./AUDIT.md).

---

## 3. Архитектура и данные

- [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md) — диаграммы
- [docs/TECHNICAL_ARCHITECTURE.md](./docs/TECHNICAL_ARCHITECTURE.md) — тех. архитектура
- [docs/DB_SCHEMA.md](./docs/DB_SCHEMA.md) · [docs/DB_ER.md](./docs/DB_ER.md) — схема БД
- [docs/TRACES.md](./docs/TRACES.md) — каталог трейсов пайплайна
- [AGENTS.md](./AGENTS.md) — ориентация по репе для агентов (что где лежит)
- [DEPLOY.md](./DEPLOY.md) — запуск и деплой

## 4. Конвенция ID

См. [docs/specs/shared/REQUIREMENTS_GLOSSARY.md](./docs/specs/shared/REQUIREMENTS_GLOSSARY.md).
Каноничные префиксы (из кода): `FR-CR-04-*` / `FR-CR-05-*` (Note Taker + Task pipeline,
исторический поток), `FR-CB2-*` (CEO Brain Bot), `FR-CB-*` (Counterparty Briefs),
`FR-TV-*` (Task Vector), `FR-GS-*` (Google Sheets sync).

## 5. Архив

Старые монолиты и дубли — в [`docs/archive/`](./docs/archive/) (история в git цела):
`SPEC.md` (524К), `SPEC_v0.1.md`, `Spec_eng.md`, `CEO_BRAIN_SPEC_ASIS.md` (код-трейс PRD от
2026-05-27, полезен как референс), `docs/SPEC_TASK_TRACKER.md`, `docs/SPEC_NOTE_TAKER.md`,
`docs/Arch.md`.

## 6. Главные выводы аудита (2026-06-03)

1. **`.env.example` устарел системно** — задокументирован только `MEETING_WEBHOOK_URL`.
   Флаги CEO Brain, Counterparty Briefs, Task Vector, Agenda и почти все Note Taker/Task Tracker
   есть в `app/config.py`, но в `.env.example` отсутствуют. Оператор по `.env.example` фичи не
   настроит. **Фикс №1.**
2. **Дизайнерские спеки разъехались с кодом:** `SPEC_TASK_TRACKER` и `SPEC_TASK_EXTRACTOR`
   недооценивают реальность (Slack-ingest, дайджесты, Google Tasks 2-way, подписки — построены,
   но помечены TODO) и расходятся в деталях (кнопки, intent-лейблы, имя таблицы аудита, recurring).
3. **Task Vector P6 (апдейт задач) уже написан, но за флагом** `TASK_VECTOR_WRITES_ENABLED=False`.
   До включения не хватает `ops/gen_task_vector_eval.py` (калибровка τ/δ).
4. **CEO Brain: код впереди спеки** (есть FR-CB2-3.32–3.39, 4.6/4.7 вне спеки); **Cat 6
   (Operations: CLI export/backfill, health, Prometheus) не построена** (4 модуля отсутствуют);
   per-run cost cap дефолт **$1**, спека NFR-CB2-C.2 говорит $5.
5. **Meeting Agenda:** недокументированный фильтр по `direction` (FR-CR-05-192aa) в
   `open_tasks_for_recordings` **ломает собственный тест спеки**; `AGENDA_MIN_PRIOR_MEETINGS`
   дефолт 2 vs 1 в спеке.
6. **Status Tracker — spec-only**, нуль кода (нет `task_status_events`, пайплайна, `person_aliases`).
   Пересекается с Task Vector — строить поверх его движка, не заново (см. AUDIT §10).
7. **Сегодняшние спеки (Webhook, Entity Consistency) и Task Vector — совпадают с кодом** ✅.
