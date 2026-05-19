# SPEC v0.1 — CEO Brain Bot (FR-CB2-200)

> **Версия:** v0.1, 2026-05-18
> **Scope:** новый Slack-bot который (а) архивирует все сообщения из каналов где он добавлен — отдельный файл/строка per channel + per day — и (б) отвечает на @mention / DM через Anthropic Claude API с доступом к нескольким MCP-серверам (Slack, Gmail, Calendar, Drive и т.д.).
> **Источники данных:** Slack Events API (read), Anthropic Claude API (Messages API + MCP), MCP-серверы из claude.ai connector-настройки (Slack, Gmail, Calendar, Drive, Fireflies, Pitchbook, Hubspot, Telegram, Atlassian, …).
> **Зависимости:** существующий `slack_bot` модуль, новый `claude_responder` модуль, новый `slack_archive` модуль.

---

## 1. Mission

«CEO Brain Bot» — Slack-присутствие оператора. Две сцепленные капабилити:

1. **Память** — каждое сообщение из любого канала / DM где бот вложен, сохраняется в `slack-archive/{channel}/{YYYY-MM-DD}.jsonl` (плюс зеркало в PG для SQL-выборок). Архив выживает rotation Slack workspace history, позволяет операторам и боту отвечать «что вчера обсуждали в #fundraising» из реальных логов.
2. **Голос** — когда бота @mention-ят в канале ИЛИ пишут ему в DM, он вызывает Anthropic Claude (Sonnet 4.6 by default) c набором MCP-серверов из его конфигурации. Claude может ЧИТАТЬ Slack-треды через Slack MCP, Calendar через Calendar MCP, Gmail через Gmail MCP и т.д., и в конце отвечает в тот же thread где его позвали.

Итог: один универсальный assistant который (a) ничего не теряет и (b) знает что есть, потому что у него есть API-key + MCP к operator-аккаунту.

---

## 2. Клиент и пользователь

### 2.1 Основной клиент

CEO / основатель humanoid.ai (Артем) — один оператор, без внешних пользователей.

### 2.2 Роли

| Роль | Действия |
|---|---|
| Оператор | @mention CEO Brain Bot в канале или DM-ит ему. Получает ответы. Использует архив для аудита/recap. |
| Член команды | Видит ответы бота в общих каналах; может @mention бота тоже. |
| CEO Brain Bot | Архивирует все сообщения. На @mention / DM собирает контекст через MCP и отвечает Claude'ом. |

### 2.3 Контекст

- Bot запускается как daemon-thread в существующем `manager-bot-1` контейнере.
- Slack Events API (Socket Mode или HTTP Events) — два потока: `message` (archive) и `app_mention`/`message.im` (responder).
- Anthropic Claude API + MCP servers (см. FR-CB2-3.x).

### 2.4 Частота

- Сообщений в архивируемых каналах: 100-500/день (humanoid workspace).
- @mention бота: 5-30 раз в день после warmup.
- Latency требование на ответ: <30 сек до streaming first token, <90 сек до полного ответа.

### 2.5 Уровень боли

- Сегодня: Slack history рулится workspace retention. Старые сообщения теряются. Контекст «что говорили об EQT в марте» приходится ловить по @ или из памяти.
- Сегодня: ассистенты внутри Slack (типа SlackGPT) не имеют доступа к Calendar/Gmail/CRM одновременно — нет одного ассистента который видит ВСЁ что доступно оператору.

---

## 3. Проблема

### 3.1 Какую решаем

«Хочу в Slack одного assistant'а который (a) помнит ВСЁ что было сказано во всех моих каналах и (b) умеет дёрнуть Gmail/Calendar/CRM сам, без копипасты».

### 3.2 Почему важна

- Высокий объём контекста (>10 каналов с 10-50 сообщений в день).
- Decision making зависит от мульти-источникового контекста (Slack thread + Calendar event + Gmail conversation).
- Текущее решение (claude.ai web UI) — отдельная страница, нет интеграции в Slack, операторам нужно переключаться.

### 3.3 Как решает сейчас

- Slack search по каналам (ограничен retention + поиск по словам).
- claude.ai web UI с включёнными connectors — но это OUTSIDE Slack, нужно копировать вопрос/ответ.
- TG-боты у некоторых — без MCP-доступа.

### 3.4 Что не работает

- Archive не сохраняется локально → потеря после workspace retention.
- claude.ai не отвечает В Slack-треде.
- Нет «один-агент-везде».

### 3.5 Последствия

- 5-15 минут в день на context-switching между Slack и claude.ai.
- Потеря контекста по старым сообщениям.
- Дублирование вопросов / задач.

---

## 4. Решение

### 4.1 Что предлагает продукт

**Архив**: каждое `message` event из Slack пишется в:
- `slack-archive/{channel_id_or_name}/{YYYY-MM-DD}.jsonl` (hot, append-only).
- `slack_message_archive` PG-таблицу (зеркало для SQL/recall).

**Responder**: на `app_mention` (в любом канале где бот вложен) или `message.im` (DM), бот:
1. Достаёт thread context (последние N сообщений треда).
2. Открывает `messages.create()` к Anthropic API с моделью `claude-sonnet-4-6` и набором MCP server URLs (см. FR-CB2-3.x).
3. Стримит ответ обратно в Slack — first `chat_postMessage` placeholder, потом `chat_update` каждые N токенов, финальный update на полный текст.
4. Записывает full prompt + tool-uses + final response в `claude_responder_runs` PG-таблицу.

### 4.2 Как решает

1. **Discovery** — Slack Events API подписка на `message`, `app_mention`, `message.im`. Бот добавлен в нужные каналы (manual `/invite @CEO Brain Bot`). Archive захватывает всё что слышит. Responder реагирует только на @mention/DM.
2. **Archive write** — atomic append к JSONL + INSERT в PG в одной транзакции (PG primary, JSONL fallback при PG-fail).
3. **MCP-augmented Claude call** — `anthropic.messages.create(model=..., mcp_servers=[...], tools=[...])`. Anthropic управляет вызовами tools через hosted MCP. Сервер-перечень — env-конфиг (см. ниже).
4. **Response delivery** — стриминг back в Slack thread.

### 4.3 Что НЕ делаем (out of scope)

- Не делаем audio (ввод/вывод).
- Не индексируем архив в vector DB (это отдельная фича FR-CB2-200-NEXT).
- Не делаем cross-workspace (только `humanoidheadquarters`).
- Не делаем UI для управления MCP (только env-list).
- Не парсим ATTACHMENTS / files автоматически (только text body сообщения).

---

## 5. User Stories + User Flow

### 5.1 US-1 — Оператор задаёт вопрос в Slack

**Как** Артем,
**Я хочу** написать `@CEO Brain Bot напомни что мы обсуждали с EQT на прошлой неделе`,
**Чтобы** получить ответ ВНУТРИ Slack-треда без переключения вкладок.

**Flow:**

```
[Артем в #fundraising] @CEO Brain Bot напомни про EQT
        ↓
[Slack Events] app_mention → /events
        ↓
[Бот] резолвит контекст треда + последние 5 сообщений канала
        ↓
[Anthropic] messages.create(messages=[…], mcp_servers=[slack, calendar, gmail])
        ↓ tool_use: slack.search_messages "EQT"
        ↓ tool_use: calendar.list_events filter=EQT
        ↓ tool_use: gmail.search "EQT"
        ↓
[Бот] chat_postMessage placeholder "🤔 думаю…" в thread
        ↓
[Бот] streaming → chat_update каждые ~200 токенов
        ↓
[Бот] final chat_update с полным ответом и `Sources: [slack #fundraising 03/05, Calendar event 12/05, Gmail thread …]`
```

### 5.2 US-2 — Оператор делает recap-запрос в DM

**Как** Артем,
**Я хочу** в DM с ботом написать `что было важного в #engineering за последние 3 дня`,
**Чтобы** не открывать канал и не пролистывать.

**Flow** аналогичен US-1 но без app_mention (DM trigger), Claude вызывает `slack.read_channel_messages(channel=engineering, since=…)`, агрегирует.

### 5.3 US-3 — Архив сохраняется бесшумно

**Как** оператор,
**Я хочу** чтобы все сообщения каналов где бот вложен пиcались в архив,
**Чтобы** через месяц/год я смог запросить «дай мне всё что было в #board за май» даже когда Slack workspace retention уже скушал old messages.

**Flow:**

```
[X] пишет в #board "обсудили deal с …"
        ↓
[Slack Events] message → /events
        ↓
[Бот] archive_write(channel=board, ts=…, user=X, text=…, raw_payload=…)
        ↓
[Disk] append к slack-archive/board/2026-05-18.jsonl
[PG]   INSERT into slack_message_archive
```

Никаких реакций видимых пользователем — silent capture.

### 5.4 US-4 — Расследование «что говорили вчера»

**Как** Артем,
**Я хочу** запросить у бота «дай транскрипт #board за вчера»,
**Чтобы** бот сделал MCP-вызов в локальный archive (или прямо `slack.read_channel_messages`) и вернул свод.

**Flow** — US-1 с upper-bound запроса.

### 5.5 US-5 — Bot-to-bot interactions

**Как** оператор,
**Я хочу** чтобы если другой бот (`fireflies-summary-bot`) пишет в канал — это попадало в архив,
**Чтобы** ассистент мог сослаться «по Fireflies-сводке от 16/05 X сказал Y».

`subtype=bot_message` события — тоже архивируются.

---

## 6. Use Cases (BDD / Gherkin)

### Feature: Slack message archive

#### Scenario: UC-1 — Архив пишется при новом сообщении в подписанном канале

```gherkin
Given Slack-bot добавлен в канал #board
And архив пуст для today
When пользователь Alice пишет в #board "лидим раунд через TWG"
Then в slack-archive/board/2026-05-18.jsonl появляется ровно 1 строка
And в PG slack_message_archive ровно одна строка с (channel="board", user="Alice", ts=<event_ts>)
And payload содержит исходный raw event JSON
```

#### Scenario: UC-2 — Архив пишется при сообщении в DM с ботом

```gherkin
Given пользователь Артем DM-ит CEO Brain Bot
When Артем пишет "напомни про X"
Then это сообщение тоже попадает в slack-archive/D0ASY5QF6UX/YYYY-MM-DD.jsonl
And в PG появляется строка с channel_id начинающимся с "D" (DM)
```

#### Scenario: UC-3 — Архив pisha НЕ дублирует одно и то же сообщение

```gherkin
Given сообщение event_ts="1779100000.123456" уже в архиве
When Slack повторно отправляет это же event (retry)
Then в архиве остаётся ровно одна строка
And `slack_message_archive` имеет UNIQUE(channel_id, ts) constraint
```

#### Scenario: UC-4 — При недоступности PG архив падает в JSONL fallback

```gherkin
Given PG недоступен (connection error)
When приходит message event
Then JSONL-файл всё равно дописывается
And событие фиксируется как pending в локальном `slack_archive_pending.jsonl`
And cron-job каждые 5 мин ре-инсёртит pending в PG
```

#### Scenario: UC-5 — Архив включает edits и deletions

```gherkin
Given сообщение с ts=X было заархивировано
When приходит `message.changed` event для того же ts
Then в архив добавляется новая строка с subtype="changed" + новый text
And в PG поле raw_payload обновляется + edit_count++
When приходит `message.deleted` event
Then в архив добавляется строка с subtype="deleted"
And в PG поле deleted_at заполняется (НЕ удаляем физически)
```

### Feature: Claude-API responder

#### Scenario: UC-6 — @mention в канале → Claude отвечает в thread

```gherkin
Given Slack-bot добавлен в #fundraising
And у бота настроены MCP servers: slack, calendar, gmail
When Артем пишет "@CEO Brain Bot напомни про EQT"
Then бот в течение 1 сек постит placeholder ":thinking_face: думаю…" как thread reply
And бот делает messages.create() к Anthropic API с claude-sonnet-4-6
And в request payload содержится mcp_servers=[<slack>, <calendar>, <gmail>]
And в течение 30 сек первый токен ответа стримится в placeholder через chat_update
And в течение 90 сек финальный ответ полностью обновлён в том же сообщении
And ответ thread_ts == ts исходного @mention сообщения
```

#### Scenario: UC-7 — Claude вызывает MCP-tool

```gherkin
Given Claude получает запрос "что было в #board за вчера"
When Claude эмитит tool_use slack.read_channel_messages(channel=board, since=2026-05-17)
Then Anthropic вызывает hosted Slack MCP с OAuth-токеном оператора
And MCP возвращает массив сообщений
And Claude использует их для финального ответа
And в PG `claude_responder_runs` фиксируется массив всех tool_use событий
```

#### Scenario: UC-8 — Claude API падает → бот извиняется

```gherkin
Given Anthropic API возвращает 5xx
When бот пытается ответить на @mention
Then placeholder обновляется текстом «⚠️ временная ошибка, попробуй через минуту»
And в `claude_responder_runs` строка с status="failed", error=<message>
And exception не валит весь bot-process
```

#### Scenario: UC-9 — Rate-limit (429) → бот ставит retry

```gherkin
Given Anthropic возвращает 429
When бот получает retry-after header
Then placeholder обновляется «⏳ rate-limit, повторю через {N} сек»
And через {N} секунд бот делает retry
And если retry успешен — финальный ответ
And если 3 retry подряд fail — сообщение об ошибке как в UC-8
```

#### Scenario: UC-10 — DM-сообщение → Claude отвечает БЕЗ @mention

```gherkin
Given пользователь открыл DM с CEO Brain Bot
When пользователь пишет "статус по EQT"
Then бот реагирует так же как на @mention (UC-6 / UC-7)
And response thread_ts отсутствует (это просто follow-up DM, не thread)
```

#### Scenario: UC-11 — Claude НЕ отвечает в каналах БЕЗ @mention

```gherkin
Given пользователь пишет в #board "обсудим EQT завтра"
And бота НЕ @mention-нули
Then responder pipeline НЕ срабатывает
And сообщение МОЛЧА уходит ТОЛЬКО в архив (UC-1)
```

#### Scenario: UC-12 — Прошлые сообщения треда подаются как контекст

```gherkin
Given thread в #fundraising содержит 5 сообщений
And последнее — @mention бота
When responder pipeline запускается
Then в messages.create() передаются последние 5 thread messages как conversation history
And first message в payload — system prompt с инструкцией CEO Brain Bot persona
```

#### Scenario: UC-13 — Бот не отвечает на свои собственные сообщения

```gherkin
Given бот опубликовал сообщение в #board
When это сообщение приходит как event обратно
Then responder pipeline видит user_id=bot и СРАЗУ выходит
And архивирование тоже скипается (или помечается subtype="self")
```

---

## 7. Архитектура

### 7.1 Высокоуровневая

```
┌─────────────────────────────────────────────────────────────┐
│             manager-bot-1 (Docker)                          │
│  ┌──────────────────┐    ┌────────────────────────────┐    │
│  │ slack_socket     │    │ existing CounterpartyBrief │    │
│  │ (Socket Mode)    │    │ AgendaRunner ...           │    │
│  └──────────────────┘    └────────────────────────────┘    │
│        │ events                                              │
│        ▼                                                     │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ slack_event_dispatcher                               │   │
│  │  ├── archive_handler (UC-1..UC-5)                   │   │
│  │  └── responder_handler (UC-6..UC-13)                │   │
│  └──────────────────────────────────────────────────────┘   │
│        │                                  │                  │
│        ▼                                  ▼                  │
│  ┌──────────────────┐         ┌─────────────────────────┐   │
│  │ slack_archive    │         │ claude_responder        │   │
│  │  ├── jsonl_sink  │         │  ├── prompt builder     │   │
│  │  └── pg_sink     │         │  ├── anthropic.client   │   │
│  └──────────────────┘         │  ├── mcp_servers config │   │
│        │                       │  └── slack streamer     │   │
│        ▼                       └─────────────────────────┘   │
│  ┌──────────────────┐                                        │
│  │ slack-archive/   │                                        │
│  │   {channel}/     │                                        │
│  │     YYYY-MM-DD.  │                                        │
│  │     jsonl        │                                        │
│  └──────────────────┘                                        │
│        │                                                     │
│        ▼                                                     │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ PostgreSQL                                            │   │
│  │  ├── slack_message_archive                           │   │
│  │  └── claude_responder_runs                           │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### 7.2 Slack-bot scopes (нужны в App Manifest)

| Scope | Зачем |
|---|---|
| `app_mentions:read` | listen for @mention |
| `channels:history` | read messages in public channels |
| `groups:history` | read in private channels (где invite-нули) |
| `im:history` | read DM messages |
| `im:read` / `mpim:read` | list DMs |
| `chat:write` | post replies |
| `chat:write.public` | post в каналы где бот не вложен (на случай recap-кросс-канал) |
| `reactions:read` / `reactions:write` | (optional) react с :thinking_face: до ответа |
| `users:read` | resolve user_id → display name для архива |

### 7.3 MCP servers config

Operator-pinned 2026-05-18: **«все что в антропик»** — то есть весь
набор connectors с claude.ai operator-аккаунта. Для v0.1 список
фиксированный (из скриншота operator settings):

| Connector | Type |
|---|---|
| Slack | built-in (Anthropic-hosted) |
| Gmail | built-in |
| Google Calendar | built-in |
| Google Drive | built-in |
| Atlassian | built-in |
| Fireflies | built-in |
| PitchBook Premium | built-in |
| Zapier | built-in |
| Отправка сообщения в телеграм | custom (n8n) |
| Artem_Gmail_n8n | custom |
| Artem_notes | custom |
| Ashby — HR Data base connector | custom |
| fundraising_data | custom |
| Google Drive N8N | custom |
| Humanoid_meetings | custom |
| MCP HuntFlow | custom |
| MCP Rocketreach n8n | custom |
| MCP Telegram Linkedin | custom |
| MCP_Telegram Chats | custom |
| Petrenko_Hubspot_search | custom |

`MCP_SERVERS` — JSON env-var с полным списком URLs / тип transport
/ OAuth-токен per server:
```json
[
  {"name":"slack",     "url":"https://mcp.slack.com/...", "type":"sse", "auth":"oauth_token:..."},
  {"name":"calendar",  "url":"...", "type":"sse"},
  ...
]
```

Передаётся в Anthropic API как `mcp_servers` параметр в каждом
`messages.create()` вызове.

OAuth-токены добываются один раз через claude.ai connector flow и
хранятся в Secret Manager (prod) / `.env` (dev). При expiration —
manual refresh через connector UI; v0.1 не делает auto-refresh.

---

## 8. Functional Requirements

### Категория 1 — Slack Events ingestion (FR-CB2-1.x)

| ID | Требование | Тест |
|---|---|---|
| FR-CB2-1.1 | Slack Events API подключение через Socket Mode (preferred) ИЛИ HTTP Events с публичным URL | `test_brain_socket_connect_ok` |
| FR-CB2-1.2 | Подписка на `message`, `app_mention`, `message.im`, `message.changed`, `message.deleted` event types | `test_brain_event_subscriptions_cover_required` |
| FR-CB2-1.3 | Bot reconnects на disconnect с exponential backoff (2/4/8/16 сек) | `test_brain_socket_reconnect_with_backoff` |
| FR-CB2-1.4 | Bot signature verification (HMAC-SHA256 с signing secret) ДЛЯ HTTP Events varianта | `test_brain_signature_verification_rejects_invalid` |
| FR-CB2-1.5 | Дедупликация Slack retry — `(event_id, ts)` идемпотентны (UC-3) | `test_brain_dedup_repeat_event` |
| FR-CB2-1.6 | Bot НЕ обрабатывает свои собственные сообщения (UC-13) | `test_brain_skips_self_messages` |
| FR-CB2-1.7 | History-poller backstop: при WebSocket reconnect (Socket Mode supervisor пере-вызывает `_attach_handlers`) НЕ накапливаются дубликаты поллера — singleton per channel-set, повторный `start_history_poller_singleton()` no-op. | `test_history_poller_singleton_no_duplicates_on_reconnect` |
| FR-CB2-1.8 | In-process responder dedup: даже при гонке между Socket-Mode push и history-poller (оба passes archive UNIQUE до commit'а), responder вызывается строго один раз на `(channel, ts)`. TTL дедупа — 60 секунд (хватает на запоздалые retry). | `test_dispatcher_responder_in_process_dedup` |

### Категория 2 — Slack archive (FR-CB2-2.x)

| ID | Требование | Тест |
|---|---|---|
| FR-CB2-2.1 | Каждое `message` пишется в `slack-archive/{channel}/{YYYY-MM-DD}.jsonl` одной строкой JSON | `test_archive_jsonl_append` |
| FR-CB2-2.2 | Параллельно — INSERT в PG `slack_message_archive(channel_id, ts, user_id, text, day, raw_payload, subtype)` | `test_archive_pg_insert` |
| FR-CB2-2.3 | UNIQUE constraint на `(channel_id, ts)` — дубли silently ignored (UC-3) | `test_archive_unique_channel_ts` |
| FR-CB2-2.4 | `message.changed` → новая JSONL-строка с `subtype="changed"` + UPDATE текста в PG row (UC-5) | `test_archive_edit_overwrites_pg_keeps_jsonl_history` |
| FR-CB2-2.5 | `message.deleted` → JSONL-строка с `subtype="deleted"` + `deleted_at` поле в PG (НЕ физическое удаление) | `test_archive_soft_delete` |
| FR-CB2-2.6 | При PG down — JSONL пишется + pending-row в `slack_archive_pending.jsonl`, cron каждые 5 мин ре-инсёртит (UC-4) | `test_archive_pg_fail_pending_replay` |
| FR-CB2-2.7 | Поддерживается архив DM, group DM (`mpim`), private channel, public channel | `test_archive_supports_all_channel_types` |
| FR-CB2-2.8 | Bot-messages (`subtype="bot_message"`) тоже архивируются (US-5) | `test_archive_includes_bot_messages` |
| FR-CB2-2.9 | Files / attachments — только metadata (file_id, name, size, mime) в архив, не сам binary | `test_archive_files_metadata_only` |
| FR-CB2-2.10 | Display-name resolution: `user_id → display_name` через `users.info` cache (LRU 5000) | `test_archive_user_display_name_cached` |
| FR-CB2-2.11 | Channel-name resolution: `channel_id → channel_name` через `conversations.info` cache (LRU 1000) | `test_archive_channel_name_cached` |
| FR-CB2-2.12 | JSONL файл имеет permissions 0640, owner=app:app — operator может читать, world — нет | `test_archive_file_permissions` |

### Категория 3 — Claude responder (FR-CB2-3.x)

| ID | Требование | Тест |
|---|---|---|
| FR-CB2-3.1 | `app_mention` event → trigger responder pipeline (UC-6) | `test_responder_triggered_by_mention` |
| FR-CB2-3.2 | `message.im` event → trigger responder pipeline, top-level OR thread-reply (UC-10) | `test_responder_triggered_by_dm` + `test_responder_triggered_by_dm_thread_reply` |
| FR-CB2-3.3 | Message без @mention в публичном канале → responder НЕ запускается (UC-11) | `test_responder_silent_on_channel_message_without_mention` |
| FR-CB2-3.4 | Placeholder сообщение ":thinking_face: думаю…" появляется в thread в <1 сек от event-receive | `test_responder_placeholder_under_1s` |
| FR-CB2-3.5 | `messages.create` к Anthropic с model=`claude-sonnet-4-6` (env-override `CEO_BRAIN_MODEL`) | `test_responder_calls_anthropic_with_default_model` |
| FR-CB2-3.6 | В payload передаётся `mcp_servers=[<…>]` из env `MCP_SERVERS` JSON | `test_responder_passes_mcp_servers` |
| FR-CB2-3.7 | Prompt caching включён для system prompt + thread history (≥1024 tokens cache breakpoint) | `test_responder_uses_prompt_caching` |
| FR-CB2-3.8 | Streaming: ответ пишется в placeholder через `chat_update` каждые ~200 токенов | `test_responder_streams_updates` |
| FR-CB2-3.9 | Финальный `chat_update` содержит полный текст + блок `Sources:` если были tool_use вызовы | `test_responder_sources_block_includes_tool_uses` |
| FR-CB2-3.10 | Thread context: подаются последние `CEO_BRAIN_THREAD_CONTEXT_MSGS` сообщений (default 10) | `test_responder_supplies_thread_history` |
| FR-CB2-3.11 | `claude_responder_runs(id, slack_channel, slack_ts, request_payload jsonb, response_text, tool_uses jsonb, status, error, created_at, completed_at)` PG-таблица — запись на каждую попытку | `test_responder_persists_run` |
| FR-CB2-3.12 | Anthropic 5xx → placeholder обновляется ":warning: ошибка" + status=failed (UC-8) | `test_responder_5xx_marks_failed` |
| FR-CB2-3.13 | Anthropic 429 → retry с retry-after header, до 3 попыток (UC-9) | `test_responder_429_retries` |
| FR-CB2-3.14 | System prompt включает CEO Brain persona + текущую дату + operator-name | `test_responder_system_prompt_includes_persona_and_date` |
| FR-CB2-3.15 | Tool-use events отображаются в Slack как промежуточные «🔍 ищу в Calendar…» (optional, env-flag) | `test_responder_tool_use_events_streamed` |
| FR-CB2-3.16 | Local Slack tools — responder регистрирует набор Anthropic-`tools` для Slack-операций (search / history / replies / post / users / channels / permalink), исполняет их локально через `slack_sdk` с операторскими токенами. Поверх `mcp.slack.com` использовать нельзя (требует Anthropic-managed OAuth); локальные tools работают параллельно с `mcp_servers` без конфликта. Multi-turn loop: после tool_use stream продолжается до `end_turn` (max `max_tool_loops=10` итераций). `search.messages` использует `CEO_BRAIN_SLACK_USER_TOKEN` (xoxp), остальные — bot token. | `test_responder_includes_local_slack_tools` + `test_responder_local_tool_use_loop` + `test_slack_tools_schemas_shape` + `test_slack_tool_search_disabled_without_user_token` |
| FR-CB2-3.17 | Synthesis recovery — когда stream заканчивается с `tool_uses` (MCP или local) но БЕЗ синтезирующего текста (наблюдаемый Sonnet-quirk: сделал tool-вызовы и end_turn без summary), responder делает ОДИН follow-up `messages.stream` call с original messages + assistant turn + user turn («Сформулируй ответ на основе результатов выше»), чтобы вынудить текстовый ответ. Если и recovery вернул пусто — рендерим явный fallback `_(модель не сформулировала ответ — см. Sources)_` вместо пустого тела. | `test_responder_synthesis_recovery_when_text_empty` |

### Категория 4 — MCP integration (FR-CB2-4.x)

| ID | Требование | Тест |
|---|---|---|
| FR-CB2-4.1 | MCP-config грузится из env `MCP_SERVERS` (JSON array). На стартапе валидируется shape | `test_mcp_config_loaded_and_validated` |
| FR-CB2-4.2 | Поддерживается тип SSE и HTTP MCP transport | `test_mcp_supports_sse_and_http` |
| FR-CB2-4.3 | OAuth-токен пер-сервер хранится в Secret Manager (GCP) / в env (dev) | `test_mcp_oauth_token_resolution` |
| FR-CB2-4.4 | При unreachable MCP server — Claude получает error response от tool_use, остальные tools работают | `test_mcp_partial_failure_degraded` |
| FR-CB2-4.5 | MCP-вызовы логируются в `claude_responder_runs.tool_uses` JSONB | `test_mcp_calls_persisted` |

### Категория 5 — Configuration / feature flags (FR-CB2-5.x)

| ID | Требование | Тест |
|---|---|---|
| FR-CB2-5.1 | `CEO_BRAIN_ENABLED=false` (default) → ни archive ни responder thread не стартуют | `test_brain_disabled_no_threads` |
| FR-CB2-5.2 | `CEO_BRAIN_ARCHIVE_ONLY=true` → archive thread работает, responder thread — нет | `test_brain_archive_only_mode` |
| FR-CB2-5.3 | `CEO_BRAIN_ANTHROPIC_API_KEY` — обязательный если ENABLED=true и не ARCHIVE_ONLY | `test_brain_responder_requires_anthropic_key` |
| FR-CB2-5.4 | `CEO_BRAIN_ARCHIVE_DIR` — путь к JSONL-архиву (default `/var/lib/manager/slack-archive`) | `test_brain_archive_dir_configurable` |
| FR-CB2-5.5 | `CEO_BRAIN_SLACK_APP_TOKEN` / `CEO_BRAIN_SLACK_BOT_TOKEN` — отдельные токены, не пересекаются с другими bot-инстансами | `test_brain_uses_dedicated_tokens` |
| FR-CB2-5.6 | `CEO_BRAIN_ARCHIVE_CHANNELS` — whitelist (CSV), пусто = архивировать всё что слышим | `test_brain_archive_whitelist` |

### Категория 6 — Operations (FR-CB2-6.x)

| ID | Требование | Тест |
|---|---|---|
| FR-CB2-6.1 | CLI `ops/brain_archive_export.py --channel X --from DATE --to DATE` — выгрузка архива в файл | `test_brain_cli_export` |
| FR-CB2-6.2 | CLI `ops/brain_backfill.py --channel X --since DATE` — backfill через `conversations.history` API для каналов добавленных задним числом | `test_brain_cli_backfill` |
| FR-CB2-6.3 | Health-check endpoint показывает archive lag (последний event time vs now) | `test_brain_health_archive_lag` |
| FR-CB2-6.4 | Prometheus метрики: `brain_archive_messages_total`, `brain_responder_runs_total`, `brain_responder_latency_seconds` | `test_brain_metrics_emitted` |

---

## 9. Non-Functional Requirements

| ID | Требование | Цель |
|---|---|---|
| NFR-CB2-P.1 | Archive write latency ≤ 100ms per message (jsonl append + PG INSERT) | per-message benchmark |
| NFR-CB2-P.2 | Responder placeholder latency ≤ 1 сек от event-receive | UC-6 timing |
| NFR-CB2-P.3 | Responder first-token latency ≤ 30 сек | enforced via Anthropic streaming |
| NFR-CB2-P.4 | Responder full-response latency ≤ 90 сек | enforced via Anthropic timeout |
| NFR-CB2-R.1 | Один failed responder run НЕ блокирует archive pipeline | разные threads / try-except |
| NFR-CB2-R.2 | Slack reconnect не теряет события — Socket Mode garantee + PG-pending fallback | NFR-tested via inject-disconnect |
| NFR-CB2-R.3 | Анти-loop guard — bot не отвечает на свои сообщения (UC-13) | hard-coded user_id skip |
| NFR-CB2-S.1 | Anthropic key и Slack bot token читаются ТОЛЬКО из env / Secret Manager, никогда из логов | grep audit |
| NFR-CB2-S.2 | Archive JSONL — `chmod 640`, owner=app:app | `test_archive_file_permissions` |
| NFR-CB2-S.3 | PG `claude_responder_runs.request_payload` НЕ содержит `mcp_servers[].auth_token` (только server-name) | `test_responder_run_payload_scrubbed` |
| NFR-CB2-C.1 | Anthropic cost ≤ $50/день под average load (≤ 50 responder runs/day × ~$1 each) | cost-tracking dashboard |
| NFR-CB2-C.2 | Per-run hard cap: ≤ $5 (env `CEO_BRAIN_MAX_RUN_COST_USD`) — Claude API max_tokens enforcement | `test_brain_per_run_cost_cap` |
| NFR-CB2-A.1 | Archive retention — 365 дней JSONL, бессрочно в PG | env `CEO_BRAIN_JSONL_RETENTION_DAYS` |

---

## 10. Data Model

### `slack_message_archive`

| Column | Type | Notes |
|---|---|---|
| id | bigserial PK | |
| channel_id | text NOT NULL | Slack channel ID (`C…` / `D…` / `G…`) |
| channel_name | text | cached for SQL convenience |
| ts | text NOT NULL | Slack event ts |
| thread_ts | text | parent thread ts if reply |
| user_id | text | author Slack user id |
| user_display_name | text | cached |
| subtype | text | NULL / `bot_message` / `changed` / `deleted` |
| text | text | message body (post-edit) |
| raw_payload | jsonb | full Slack event payload |
| edit_count | int default 0 | bumped on `message.changed` |
| deleted_at | timestamptz | NULL unless `message.deleted` received |
| day | date NOT NULL | partition key |
| created_at | timestamptz | first time we saw this message |
| updated_at | timestamptz | last update |
| **UNIQUE** | (channel_id, ts) | — FR-CB2-2.3 |

### `claude_responder_runs`

| Column | Type | Notes |
|---|---|---|
| id | bigserial PK | |
| slack_channel_id | text NOT NULL | where the @mention/DM happened |
| slack_event_ts | text NOT NULL | trigger event ts |
| slack_placeholder_ts | text | ts of our placeholder message (for chat_update) |
| request_payload | jsonb | scrubbed Anthropic request |
| response_text | text | final assistant response |
| tool_uses | jsonb | array of {name, input, output_summary} |
| status | text | `pending` / `streaming` / `done` / `failed` / `rate_limited` |
| error | text | filled on failed |
| cost_usd | numeric(10,4) | per-run cost from Anthropic usage |
| input_tokens | int | |
| output_tokens | int | |
| cache_read_tokens | int | |
| cache_write_tokens | int | |
| started_at | timestamptz | |
| completed_at | timestamptz | |

---

## 11. Slack App Manifest (delta vs current)

New scopes needed in addition to existing slack-task-bot:
- `app_mentions:read`
- `channels:history`
- `groups:history`
- `im:history`
- `mpim:history`
- `users:read`

Event subscriptions:
- `app_mention`
- `message.channels`
- `message.groups`
- `message.im`
- `message.mpim`
- (subtype filters: `message_changed`, `message_deleted` — wildcard)

---

## 12. Test Traceability Matrix

| FR/NFR ID | Test file::test name |
|---|---|
| FR-CB2-1.1 | `tests/requirements/test_ceo_brain.py::test_brain_socket_connect_ok` |
| FR-CB2-1.2 | `…::test_brain_event_subscriptions_cover_required` |
| FR-CB2-1.3 | `…::test_brain_socket_reconnect_with_backoff` |
| FR-CB2-1.4 | `…::test_brain_signature_verification_rejects_invalid` |
| FR-CB2-1.5 | `…::test_brain_dedup_repeat_event` |
| FR-CB2-1.6 | `…::test_brain_skips_self_messages` |
| FR-CB2-1.7 | `…::test_history_poller_singleton_no_duplicates_on_reconnect` |
| FR-CB2-1.8 | `…::test_dispatcher_responder_in_process_dedup` |
| FR-CB2-2.1 | `…::test_archive_jsonl_append` |
| FR-CB2-2.2 | `…::test_archive_pg_insert` |
| FR-CB2-2.3 | `…::test_archive_unique_channel_ts` |
| FR-CB2-2.4 | `…::test_archive_edit_overwrites_pg_keeps_jsonl_history` |
| FR-CB2-2.5 | `…::test_archive_soft_delete` |
| FR-CB2-2.6 | `…::test_archive_pg_fail_pending_replay` |
| FR-CB2-2.7 | `…::test_archive_supports_all_channel_types` |
| FR-CB2-2.8 | `…::test_archive_includes_bot_messages` |
| FR-CB2-2.9 | `…::test_archive_files_metadata_only` |
| FR-CB2-2.10 | `…::test_archive_user_display_name_cached` |
| FR-CB2-2.11 | `…::test_archive_channel_name_cached` |
| FR-CB2-2.12 | `…::test_archive_file_permissions` |
| FR-CB2-3.1 | `…::test_responder_triggered_by_mention` |
| FR-CB2-3.2 | `…::test_responder_triggered_by_dm` |
| FR-CB2-3.3 | `…::test_responder_silent_on_channel_message_without_mention` |
| FR-CB2-3.4 | `…::test_responder_placeholder_under_1s` |
| FR-CB2-3.5 | `…::test_responder_calls_anthropic_with_default_model` |
| FR-CB2-3.6 | `…::test_responder_passes_mcp_servers` |
| FR-CB2-3.7 | `…::test_responder_uses_prompt_caching` |
| FR-CB2-3.8 | `…::test_responder_streams_updates` |
| FR-CB2-3.9 | `…::test_responder_sources_block_includes_tool_uses` |
| FR-CB2-3.10 | `…::test_responder_supplies_thread_history` |
| FR-CB2-3.11 | `…::test_responder_persists_run` |
| FR-CB2-3.12 | `…::test_responder_5xx_marks_failed` |
| FR-CB2-3.13 | `…::test_responder_429_retries` |
| FR-CB2-3.14 | `…::test_responder_system_prompt_includes_persona_and_date` |
| FR-CB2-3.15 | `…::test_responder_tool_use_events_streamed` |
| FR-CB2-3.16 | `…::test_responder_includes_local_slack_tools` + `…::test_responder_local_tool_use_loop` + `…::test_slack_tools_schemas_shape` + `…::test_slack_tool_search_disabled_without_user_token` |
| FR-CB2-3.17 | `…::test_responder_synthesis_recovery_when_text_empty` |
| FR-CB2-4.1 | `…::test_mcp_config_loaded_and_validated` |
| FR-CB2-4.2 | `…::test_mcp_supports_sse_and_http` |
| FR-CB2-4.3 | `…::test_mcp_oauth_token_resolution` |
| FR-CB2-4.4 | `…::test_mcp_partial_failure_degraded` |
| FR-CB2-4.5 | `…::test_mcp_calls_persisted` |
| FR-CB2-5.1 | `…::test_brain_disabled_no_threads` |
| FR-CB2-5.2 | `…::test_brain_archive_only_mode` |
| FR-CB2-5.3 | `…::test_brain_responder_requires_anthropic_key` |
| FR-CB2-5.4 | `…::test_brain_archive_dir_configurable` |
| FR-CB2-5.5 | `…::test_brain_uses_dedicated_tokens` |
| FR-CB2-5.6 | `…::test_brain_archive_whitelist` |
| FR-CB2-6.1 | `…::test_brain_cli_export` |
| FR-CB2-6.2 | `…::test_brain_cli_backfill` |
| FR-CB2-6.3 | `…::test_brain_health_archive_lag` |
| FR-CB2-6.4 | `…::test_brain_metrics_emitted` |
| NFR-CB2-P.1..P.4 | manual benchmark + manual test |
| NFR-CB2-R.1 | `…::test_brain_failed_run_does_not_block_archive` |
| NFR-CB2-R.2 | `…::test_brain_reconnect_no_event_loss` (with inject-disconnect harness) |
| NFR-CB2-R.3 | `…::test_brain_skips_self_messages` (same as FR-CB2-1.6) |
| NFR-CB2-S.1 | `tests/security/test_no_secret_in_logs.py` (existing pattern) |
| NFR-CB2-S.2 | `…::test_archive_file_permissions` |
| NFR-CB2-S.3 | `…::test_responder_run_payload_scrubbed` |
| NFR-CB2-C.1 | dashboard alert config |
| NFR-CB2-C.2 | `…::test_brain_per_run_cost_cap` |
| NFR-CB2-A.1 | `…::test_brain_jsonl_retention_rotate` |

---

## 13. Operator decisions (2026-05-18)

| Вопрос | Решение оператора |
|---|---|
| Когда бот отвечает? | «когда пишу в него» — @mention в канале ИЛИ DM. На любые другие сообщения молчит (UC-11). |
| Какие MCP сервера? | «все что в антропик» — full set из claude.ai connectors (см. §7.3 table). |
| Архив scope | по умолчанию ВСЕ каналы где бот вложен (UC-1..UC-5). Whitelist через env (FR-CB2-5.6) если нужно сузить позже. |
| Storage | JSONL + PG mirror (FR-CB2-2.1 + 2.2). |

## 14. Open Questions

1. **Single bot or two?** Делать «CEO Brain Bot» отдельным Slack App от existing slack-task-bot или подмешивать в один app? Решение: **отдельный app** — изоляция scopes, ключей, retry-логики. Existing slack-task-bot пишет в один канал; CEO Brain должен жить во всех.
2. **Socket Mode vs HTTP Events?** Socket Mode проще (нет публичного URL) — рекомендую начать с него. HTTP можно добавить позже.
3. **Vector-search архива?** Out of scope v0.1. После накопления 30+ дней данных — отдельный спринт.
4. **MCP server hosting** — claude.ai connectors используют hosted MCP под капотом. Можем ли мы дёрнуть те же URLs из API? Нужна верификация с Anthropic docs / поддержкой. Fallback — self-host MCP servers для каждого connector.
5. **Bot reply persona** — нужны guidelines (тон, длина, когда не отвечать). Документировать отдельно в `CEO_BRAIN_PERSONA.md` после v0.1.
6. **Опцион — bot reactions** — `:thinking_face:` пока работает, потом `:white_check_mark:` когда finished. Полезно?
7. **OAuth refresh для custom connectors** — n8n flows истекают; ручной refresh OK для v0.1 или сразу автоматизировать?

---

## 15. Rollout plan

| Этап | Что делаем | Критерий successful |
|---|---|---|
| Sprint 1 | Slack archive only (FR-CB2-1.x + 2.x + 5.1/5.2/5.4/5.6) | архив пишется 7 дней без потерь |
| Sprint 2 | Responder skeleton (FR-CB2-3.1-3.6) — без MCP, plain Claude call | бот отвечает в DM на тестовом канале |
| Sprint 3 | MCP integration (FR-CB2-4.x) — start with Slack MCP only | бот может сделать recall по архиву через Slack MCP |
| Sprint 4 | Multi-MCP + streaming + persistence (FR-CB2-3.7-3.15) | end-to-end на проде |
| Sprint 5 | Ops + metrics (FR-CB2-6.x) | dashboard live, alerts wired |

---

## 16. Open Items Before Coding

- [ ] **Operator approval** этого SPEC v0.1
- [ ] **Slack App** новый workspace install + manifest review
- [ ] **MCP server URLs** — выгрузить из claude.ai connector settings (нужен доступ к operator account)
- [ ] **Anthropic API key** в Secret Manager / env (НЕ в коде)
- [ ] **PG migration** для двух новых таблиц
- [ ] **Tests stub** все 50+ тестов с `pytest.mark.xfail(strict=True)` ДО реализации (TDD)
