# SPEC — Task Tracker Agent

> **Статус:** draft v1, 2026-05-08
> **Owner:** Артём Соколов (CEO Office)
> **Mission:** Управлять полным жизненным циклом задач — от извлечения из любого канала коммуникации до закрытия. Главный клиент: **Telegram-карточка с кнопками** + **двухсторонняя синхронизация с Google Tasks**.

## 1. Scope

**В scope:**
- Извлечение задач из: Telegram (live + история), Slack (live), Email, Note Taker (meeting tasks), Manual (от админа в TG)
- Auto-assign owner через `team_members` справочник (LLM matching)
- Auto-detect deadline (LLM date parsing с relative «к пятнице»)
- Statuses: `todo / in_progress / blocked / done / cancelled`
- TG-карточки с кнопками: `Принять / Делегировать / Отложить / Закрыть / Изменить дедлайн`
- Морnings/evening дайджесты в TG для admin'ов
- Deadline reminders (за день / в день / просрочено)
- Recurring tasks (daily / weekly / monthly)
- Двухсторонняя sync с Google Tasks
- Dedup задач (fuzzy matching) поперёк всех источников
- Subscription system: «подписаться на чужую задачу»

**Вне scope:**
- Транскрибация / summary встреч → Note Taker
- Внутренний messaging (это просто слой над уже-существующими каналами)
- Project management features (Gantt, dependencies, sprints) — слишком далеко

## 2. Sources (где появляются задачи)

| Источник | Транспорт | Триггер | Статус |
|---|---|---|---|
| **Telegram** | Long-poll bot API + Supabase view (история чатов) | Любое сообщение в подписанных чатах → LLM-classify «task or chitchat» | ✅ работает |
| **Slack** | Socket Mode (Bolt) | Любое сообщение в каналах где бот, + @mentions | ⏳ TODO (см. SPEC) |
| **Email** | IMAP poll или Gmail API push | Inbox-фильтр по адресам / labels | ⏳ TODO |
| **Note Taker** | DB read из `tasks` где `source_kind ∈ (zoom, fireflies, gmeet, manual)` | После завершения pipeline'a встречи — автоматически | ✅ работает (через общий DB) |
| **Manual** | TG-команда `/task <текст>` от admin | По запросу | ⏳ TODO |
| **Recurring scheduler** | Cron внутри listener | Каждые N минут проверяем due recurring rows | ⏳ TODO |

## 3. Pipeline (для каждого источника одинаковый)

```
┌────────────┐
│ Inbound    │  (TG message / Slack message / Email / Meeting tasks / Recurring trigger)
│ message    │
└─────┬──────┘
      │
      v
┌──────────────────────────────────────────────────────────┐
│ Classify (LLM):                                           │
│   intent ∈ {task, chitchat, question, status_update}      │
│   confidence: 0.0-1.0                                     │
│ → если task — продолжаем                                  │
└─────┬─────────────────────────────────────────────────────┘
      v
┌──────────────────────────────────────────────────────────┐
│ Drafts loop (LLM-graph): для каждого detected task:       │
│                                                           │
│ - title_node       — короткое название (≤60 chars)       │
│ - description_node — подробности (≤500 chars)            │
│ - owner_node       — auto-assign из team_members          │
│ - date_node        — parse «к пятнице», «через 3 дня»     │
│ - priority_node    — low/medium/high/urgent               │
│ - dedup_node       — поиск похожих в БД, skip если дубль  │
└─────┬─────────────────────────────────────────────────────┘
      v
┌──────────────────────────────────────────────────────────┐
│ Persist Task row:                                         │
│   source_kind, source_conversation_id, source_permalink   │
│   title, description, owner_user_id, owner_display_name   │
│   due_date, due_time, priority, status='todo'             │
│   created_by_slack_user_id (полиморфный — тут TG uid)     │
└─────┬─────────────────────────────────────────────────────┘
      v
┌──────────────────────────────────────────────────────────┐
│ Distribute (TG-карточки):                                 │
│   recipients = author + owner + admins (deduped)          │
│   карточка с кнопками для каждого:                        │
│   ┌──────────────────────────────────────────────┐        │
│   │ 📌 [title]                                    │        │
│   │ 👤 Owner: @username                           │        │
│   │ 📅 Due: 2026-05-12 (через 4 дня)              │        │
│   │ 🟠 Priority: high                             │        │
│   │ 🟡 Status: TODO                               │        │
│   │ 📝 [description first 200 chars...]           │        │
│   │ 🔗 Source: t.me/c/.../msg                     │        │
│   │                                                │        │
│   │ [✅ Принять]  [➡️ Делегировать]                │        │
│   │ [⏰ Отложить] [✏️ Изменить] [❌ Закрыть]        │        │
│   └──────────────────────────────────────────────┘        │
└──────────────────────────────────────────────────────────┘
      v
┌──────────────────────────────────────────────────────────┐
│ Sync (background, throttled):                             │
│   - Google Tasks: create/update/delete отражает status    │
│   - Webhook (если configured): POST JSON на каждом change │
└──────────────────────────────────────────────────────────┘
```

## 4. Auto-routing logic

### Owner resolution

1. LLM смотрит на текст сообщения + список team_members (real_name + role + notes)
2. Если в тексте явно «@username» / «Имя» — match по `team_members.telegram_username` / `real_name`
3. Если неявно — LLM выводит owner по контексту (тема + role)
4. Если нет совпадения — fallback на admin (CEO)

### Deadline parsing

LLM `date_node` со схемой:
```python
{
  "iso": "2026-05-12" | "2026-05-12T15:00" | null,
  "relative": "сегодня" | "завтра" | "к пятнице" | "через 3 дня" | null,
  "reasoning": "из текста '...' видно..."
}
```
+ Python-side validation против `datetime.now(timezone.utc)`.

### Priority inference

LLM смотрит на лексику:
- `urgent`: «срочно», «горит», «вчера было надо»
- `high`: «важно», «не забудь», «обязательно»
- `medium`: дефолт
- `low`: «когда будет время», «не срочно»

### Dedup

Существующая логика (FR-CR-05-128):
1. Topic-prefix exact match: `<topic> - <verb>` совпадает игнорируя case
2. Title fuzzy: SequenceMatcher ≥ 0.85
3. Owner overlap: тот же owner (если есть)

При срабатывании — skip new draft, отметить link на existing.

## 5. TG-карточка: жизненный цикл

### Состояния

```
TODO → IN_PROGRESS → DONE (закрыта)
              ├──→ BLOCKED (с reason)
              └──→ CANCELLED
```

### Кнопки и actions

| Кнопка | Кто видит | Действие |
|---|---|---|
| ✅ Принять | Owner | status: TODO → IN_PROGRESS |
| ➡️ Делегировать | Owner | Открыть inline-keyboard со списком team_members → новый owner |
| ⏰ Отложить | Owner | Inline-keyboard: «+1 день / +1 неделя / другое» → update due_date |
| ✏️ Изменить | Owner / Admin | Conversation-flow: edit title/desc/due/priority через TG messages |
| ❌ Закрыть | Owner / Admin | status → DONE, optional reason text |
| 👀 Подписаться | Любой viewer | Получает notifications о смене статуса |
| 🔄 Refresh | Любой | Перерисовать карточку с актуальным state |

При каждом edit — карточка **обновляется in-place** (edit_message), все receivers видят свежее состояние с разной keyboard (owner видит больше кнопок чем bystander).

## 6. Дайджесты и нотификации

### Утренний дайджест (08:00 локального time)

Получатели: каждый team_member с `telegram_user_id IS NOT NULL` и активными задачами.

```
🌅 Доброе утро, [Имя]!

Задачи на сегодня:
1. [TITLE] — due TODAY
   Source: ...

2. [TITLE] — due TOMORROW
   Source: ...

⏰ Просроченные:
3. [TITLE] — была due вчера

Всего: 12 active, 3 overdue.
[Открыть полный список]
```

### Вечерний дайджест (18:00)

```
🌆 Конец дня

Сделано сегодня (5 задач):
✅ ...
✅ ...

В работе (7):
🟡 ...
🟡 ...

Не начато до сегодня (3):
⏸️ ...

Завтра plan: 4 задачи.
```

### Deadline reminders

- За **день** до дедлайна (07:00 утром)
- В **день** дедлайна (09:00 + 16:00)
- При **просрочке** (на следующий день 09:00, потом раз в 3 дня)

Триггеры — cron внутри listener'a.

### Status update notifications (push, не дайджест)

Любой change статуса → DM owner + admins + subscribers:
```
🟢 [TITLE] — статус изменён: TODO → IN_PROGRESS
👤 by @username
```

## 7. Recurring tasks

Новая таблица `recurring_task_rules`:

```sql
CREATE TABLE recurring_task_rules (
  id SERIAL PRIMARY KEY,
  rule_name VARCHAR(255),
  template_title VARCHAR(255),
  template_description TEXT,
  template_owner_user_id BIGINT,
  template_priority VARCHAR(16),

  schedule_kind VARCHAR(16),    -- 'daily' / 'weekly' / 'monthly' / 'cron'
  schedule_args JSONB,          -- {"days_of_week":[1,3,5]} / {"day_of_month":15} / {"cron":"0 9 * * MON"}
  next_run_at TIMESTAMPTZ,
  enabled BOOLEAN DEFAULT TRUE,
  created_at, updated_at
);
```

Cron внутри listener'a каждые 5 минут:
- `SELECT * FROM recurring_task_rules WHERE enabled AND next_run_at <= NOW()`
- Для каждой → INSERT новую Task с template_*
- Update `next_run_at` к следующему slot'у согласно `schedule_kind`

## 8. Google Tasks two-way sync

### Push (наша → Google Tasks)

При INSERT/UPDATE Task row:
- POST/PATCH в Google Tasks API
- Mapping:
  - `tasks.title` ↔ `gtask.title`
  - `tasks.description` ↔ `gtask.notes`
  - `tasks.due_date + due_time` ↔ `gtask.due` (RFC3339)
  - `tasks.status='done'` ↔ `gtask.status='completed'`
- Store `gtask_id` в `tasks.google_task_id` для linking

### Pull (Google Tasks → наша)

Periodic poll (каждые 60s) `tasks.list` на default tasklist:
- Для каждого Google Task с `google_task_id` уже у нас → check timestamps, апдейтим если в Google новее
- Для каждого Google Task без linking → новая задача с `source_kind='google_tasks'`, owner=admin

Conflict resolution: **last-write-wins** by `updated_at` from each side.

## 9. Configuration (env)

```ini
# Все из Note Taker (OPENAI_*, TELEGRAM_*, etc) +

# Slack ingestion (новое)
SLACK_INGEST_ENABLED=false
SLACK_BOT_TOKEN=xoxb-...           # с history scopes
SLACK_APP_TOKEN=xapp-...           # для Socket Mode

# Email ingestion (новое)
EMAIL_INGEST_ENABLED=false
GMAIL_CLIENT_ID=...
GMAIL_CLIENT_SECRET=...
GMAIL_LABEL_FILTER=tasks           # обрабатываем только этот label
GMAIL_POLL_INTERVAL_SECONDS=120

# Recurring scheduler
RECURRING_TASKS_ENABLED=true
RECURRING_CHECK_INTERVAL_SECONDS=300

# Digests
MORNING_DIGEST_HOUR_LOCAL=8
EVENING_DIGEST_HOUR_LOCAL=18
TIMEZONE=Europe/London

# Reminders
DEADLINE_REMINDER_DAY_BEFORE_HOUR=7
DEADLINE_REMINDER_DAY_OF_HOURS=9,16
DEADLINE_REMINDER_OVERDUE_HOUR=9

# Google Tasks sync
GOOGLE_TASKS_DEFAULT_TASKLIST_ID=eGMyb2Y5NVAzQzBFMHludQ
GOOGLE_TASKS_PUSH_ENABLED=true
GOOGLE_TASKS_PULL_INTERVAL_SECONDS=60
```

## 10. Database

### Existing (расширяем)

`tasks` table:
```sql
id SERIAL PRIMARY KEY
source_kind VARCHAR(16)              -- 'telegram'|'slack'|'fireflies'|'zoom'|'email'|'manual'|'recurring'|'google_tasks'
source_conversation_id VARCHAR(255)  -- chat_id / channel_id / fireflies_id / etc
source_message_id VARCHAR(255)       -- ts / message_id
source_permalink VARCHAR(512)        -- t.me link / slack permalink

title VARCHAR(500)
description TEXT
owner_user_id BIGINT                 -- TG numeric uid
owner_display_name VARCHAR(255)
priority VARCHAR(16)                 -- 'low'|'medium'|'high'|'urgent'
status VARCHAR(16) DEFAULT 'todo'    -- 'todo'|'in_progress'|'blocked'|'done'|'cancelled'
due_date DATE NULL
due_time TIME NULL
google_task_id VARCHAR(64) NULL      -- для two-way sync

created_by_slack_user_id VARCHAR(64) NULL  -- legacy field, holds tg uid for tg-source
created_at TIMESTAMPTZ
updated_at TIMESTAMPTZ
deleted_at TIMESTAMPTZ NULL          -- soft delete

card_channel VARCHAR(64) NULL        -- TG chat_id первой делiverness card
card_ts VARCHAR(64) NULL             -- TG message_id первой card
extra JSONB DEFAULT '{}'             -- {telegram_cards: [{chat_id, message_id}], ...}
```

### New tables

```sql
recurring_task_rules (...)             -- см. секцию 7
task_subscriptions (
  task_id INT REFERENCES tasks(id),
  subscriber_user_id BIGINT,           -- TG uid
  subscribed_at TIMESTAMPTZ
)
task_status_changes (                  -- audit log
  id SERIAL PRIMARY KEY,
  task_id INT REFERENCES tasks(id),
  old_status, new_status,
  changed_by_user_id BIGINT,
  changed_at TIMESTAMPTZ,
  reason TEXT NULL
)
processed_telegram_messages (          -- existing, dedup
  chat_id BIGINT, message_id BIGINT,
  PRIMARY KEY (chat_id, message_id)
)
processed_slack_messages (             -- TODO для Slack ingest
  channel_id VARCHAR(64), message_ts VARCHAR(64),
  PRIMARY KEY (channel_id, message_ts)
)
processed_email_messages (             -- TODO для Email ingest
  message_id VARCHAR(255) PRIMARY KEY,
  ...
)
```

## 11. Failure modes

| Сценарий | Поведение |
|---|---|
| TG getUpdates timeout | retry в next tick, no data loss (Supabase view holds backlog) |
| Slack Socket disconnect | slack_bolt auto-reconnect (built-in) |
| Email IMAP fails | log warning, retry next interval |
| LLM 429 / network error | OpenAI SDK retry, затем skip — следующий tick подхватит ту же row |
| Owner has no /start (TG) | `chat not found` → skip recipient, log info |
| Google Tasks API quota | exponential backoff, log warning |
| Recurring rule misfires (timezone bug) | manual fix через DB edit или admin UI (TODO) |
| Container reset mid-pipeline | TG view'ы догонят на следующем poll'е (последние 500 messages); Slack — Socket reconnect; in-flight tasks остаются в DB консистентно (рисуем по transaction boundaries) |

## 12. Observability

### Логи (structlog JSON)

```
listener_view_poll_done seen=N drafts_proposed=N errors=N
telegram_prepare_drafts_loop_start message_id=N task_count=N
telegram_prepare_drafts_skipped_duplicate duplicate_of=ID reason=...
telegram_card_dm_failed task_id=N uid=N hint='...'
telegram_card_dm_succeeded task_id=N uid=N
intent_classify model=... result=task confidence=0.95
date_node_result final=2026-05-12 source=llm reasoning=...
owner_node_result final_uid=N llm_picked=... reasoning=...
recurring_task_fired rule_id=N task_id=N
google_tasks_push_done task_id=N gtask_id=...
google_tasks_pull_done seen=N updated=N created=N
deadline_reminder_sent task_id=N to_uid=N kind=day_before|day_of|overdue
digest_sent kind=morning|evening to_uid=N tasks_count=N
```

### Метрики (TODO)

- `tasks_created_total{source_kind}` counter
- `tasks_status_changed_total{from, to}` counter
- `tasks_active{owner}` gauge
- `tasks_overdue{owner}` gauge
- `tg_card_dm_failed_total{reason}` counter

### Healthcheck

`/health`:
- ✅ DB ok
- ✅ TG bot getMe ok (last <60s)
- ✅ Last view_poll < 5min
- ⚠️ Backlog of un-classified messages > 100

## 13. Deployment

### Текущее (1 контейнер)

`slack-task-tg-listener` делает всё: TG-poll, Zoom-poll, Fireflies-poll, recurring (TODO), digests (TODO).

### Целевое (раздельные роли)

```
task-tracker-tg-ingest        — TG long-poll + view-poll
task-tracker-slack-ingest     — Socket Mode  
task-tracker-email-ingest     — IMAP poll
task-tracker-scheduler        — cron-like для recurring + digests + reminders
task-tracker-gtasks-sync      — Google Tasks two-way sync worker
```

Каждый — отдельный контейнер с `--restart unless-stopped`. Если умирает scheduler, ingest'ы продолжают писать в DB.

## 14. Roadmap

| Quarter | Feature |
|---|---|
| Q2-2026 | Slack ingest (см. SPEC) |
| Q2-2026 | Email ingest (Gmail label) |
| Q2-2026 | Recurring tasks (cron-like) |
| Q2-2026 | Morning/evening digests с правильным TZ |
| Q3-2026 | Двухсторонняя Google Tasks sync с conflict resolution |
| Q3-2026 | Manual `/task` command в TG |
| Q3-2026 | Inline-edit карточек (без separate conversation flow) |
| Q3-2026 | Subscriptions: «следить за чужой задачей» |
| Q4-2026 | Web admin UI: CRUD recurring rules, browse all tasks, full-text search |
| Q4-2026 | Notion / Airtable export через webhook |

## 15. Связь с Note Taker

Note Taker сохраняет задачи в `tasks` table со source_kind ∈ {`zoom`, `fireflies`, `gmeet`, `manual`}. Task Tracker:
- Видит их через DB (один shared schema)
- Применяет ту же routing-logic (owner, deadline, priority)
- Шлёт TG-карточки получателям
- Включает в дайджесты и reminders
- Двухсторонне синкает с Google Tasks

То есть Note Taker и Task Tracker используют **одну БД**, но решают разные задачи. Task Tracker может работать без Note Taker (без meeting'ов) и наоборот (но тогда задачи из встреч не материализуются в TG).

---

**Версии:**
- v1 (2026-05-08) — initial draft, фиксирует текущее состояние TG ingest + meeting tasks ingest; Slack/Email/Recurring/Digests/2-way-sync в TODO
