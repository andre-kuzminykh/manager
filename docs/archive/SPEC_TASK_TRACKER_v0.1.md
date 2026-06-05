# SPEC v0.1 — Task Tracker Agent

> **🗄️ АРХИВ (2026-06-05).** Эта спека отстала от кода.
> Перечень расхождений — `AUDIT.md` §6 (на 2026-06-03).
> Реализованные в коде требования трассируются через `FR-CR-*` (см. `PRD.md` §4).
> Для актуального статуса фичи смотри: `AGENTS.md` + `docs/PIPELINE_FLOW.md` + код
> (`app/slack_bot/`, `app/orchestrator/`, `app/sync/`).

> **Уровень:** Product + Business + Solution Architecture (Draft)
> **Дата:** 2026-05-08
> **Целевой пользователь:** CEO, solopreneurs, предприниматели
> **Структура:** разделы 1–14 покрывают этапы продуктового, бизнес- и архитектурного анализа

---

# 1. Краткое описание продукта

**Task Tracker** — AI-агент, который автоматически извлекает задачи из любого канала коммуникации (Telegram-чаты, Slack, Email, meeting transcripts от Note Taker), назначает ответственного из справочника команды, парсит дедлайн и приоритет, шлёт TG-карточку с кнопками владельцу и admin'у, синхронизирует с Google Tasks, шлёт утренние/вечерние дайджесты и deadline reminders.

Главная ценность для CEO/solopreneur: **0 ручных действий** для перевода обещания из чата в трекинг-систему. Сообщение «Алина, отправь Mark follow-up до пятницы» в TG-чате автоматически становится задачей с owner=Алина, due=пятница, status=todo, и Алина получает в TG DM-карточку с кнопками «Принять / Делегировать / Закрыть».

---

# 2. Клиент и пользователь

## 2.1 Основной клиент

CEO / solopreneur с командой 10-30 человек:
- 60+ активных Telegram чатов
- Один Slack workspace
- Email с задачами от внешних клиентов
- Поток meeting'ов через Note Taker

## 2.2 Роли пользователей

| Роль | Кол-во | Что делает | Через какой интерфейс |
|---|---|---|---|
| **CEO (admin)** | 1 | Видит ВСЕ задачи. Может перенаправлять, закрывать, создавать вручную через `/task`. Получает дайджесты. | Telegram DM с ботом |
| **Owner задачи** (сотрудник) | 10-30 | Получает свою задачу как TG-карточку. Нажимает Принять / Делегировать / Закрыть. Получает дайджесты. | Telegram DM с ботом |
| **Subscriber** (TODO) | случайно | Подписался на чужую задачу. Получает уведомления о смене статуса. | Telegram DM |
| **External BI / consumer** | 1-N | Читает задачи через webhook или read-only DB role | Webhook + psql |

## 2.3 Контекст использования

| Когда | Что хочет |
|---|---|
| Написал в TG-чате «Алина, сделай X» | Через 30s → TG-карточка Алине + admin'у |
| Утром (08:00) | Дайджест: задачи на сегодня + просроченные |
| В течение дня | TG-карточки новых задач из чатов / встреч |
| Закрыл задачу | Status update в Google Tasks (для GTasks-power users) |
| Вечером (18:00) | Дайджест: что сделано, план на завтра |
| Дедлайн через час | Push-нотификация в TG |

## 2.4 Частота использования

- 20-50 задач/день у активного CEO
- 1-5 карточек/день у каждого owner'a
- 2 дайджеста/день для admin + active owner

## 2.5 Уровень боли пользователя

**Высокий:**
- Notion / Trello / Asana — 1-2 недели энтузиазма потом заброс
- Голос «надо сделать X» в чате — пропадает в потоке
- Дедлайны размытые («когда будет время» = никогда)
- Нет единого «что сегодня» из всех источников

---

# 3. Проблема

## 3.1 Какую проблему решаем

CEO даёт обещания и поручения в десятках мест: TG-чаты, Slack, email, Zoom. Каждое теряется в шуме. Ручной перенос в task-tracker не масштабируется. Команда не понимает приоритетов.

## 3.2 Почему она важна

- Финансово: пропущенный follow-up = lost deal
- Команда работает «в туман» без чёткого daily-list
- CEO sucks into operations вместо стратегии

## 3.3 Как решает сейчас

- **Notion / Trello**: 100% ручной ввод → энтузиазм 1-2 недели → заброс
- **Память + блокноты**: 30-50% потери
- **Ассистент-человек**: $40-80k/год, медленно, single point of failure
- **Голосовые в WhatsApp себе**: shadow-system без actionable workflow

## 3.4 Что не работает

- Ручной ввод не масштабируется на 30+ задач/день
- Никакой связи «было сказано» → «появилось в task list»
- Нет авто-назначения owner'а (приходится думать «кому это поручить»)
- Дедлайны из контекста не парсятся («к пятнице» → дата)
- Нет единого view «что мне сегодня делать»

## 3.5 Последствия

- Упущенные deals
- Demotivated team
- CEO в режиме операций
- Burnout

---

# 4. Решение

## 4.1 Что предлагает продукт

Task Tracker автоматически:
1. **Подписывается** на источники: TG (live + история через Supabase view), Slack (Q2-2026), Email (Q2-2026), Note Taker (через DB), Manual (через `/task` команду — TODO)
2. **Классифицирует** через LLM: task / chitchat / question / status_update
3. **Извлекает** задачу через drafts loop: title / description / owner (из team_members) / due (LLM-parse «к пятнице») / priority
4. **Дедуплицирует** по topic-prefix + fuzzy title
5. **Сохраняет** Task row в `tasks` table
6. **Шлёт** TG-карточку с кнопками: Принять / Делегировать / Отложить / Изменить / Закрыть / Подписаться
7. **Обновляет** карточку in-place у всех recipients при button-press
8. **Шлёт** утренние/вечерние дайджесты + deadline reminders (Q2-2026)
9. **Синхронизирует** с Google Tasks two-way (Q3-2026)
10. **Триггерит** recurring tasks по cron (Q2-2026)

## 4.2 Как продукт решает проблему

| Боль | Что делает Task Tracker |
|---|---|
| «Не успеваю мониторить 60 чатов» | TG ingest читает Supabase view каждые 30s, классифицирует, извлекает |
| «Не знаю кому поручить» | LLM owner_node автоматически выбирает из team_members по контексту |
| «Дедлайн размыт» | LLM date_node парсит «к пятнице» / «через 3 дня» / «срочно» в ISO дату |
| «Не вижу всего списка» | Утренний дайджест в TG: задачи на сегодня + просроченные (Q2) |
| «Контекст теряется между каналами» | Один Postgres для всех источников, единый pipeline |
| «Запутался кто делает что» | Карточка обновляется in-place, аудит в `task_status_changes` |

## 4.3 Почему это лучше текущего способа

| Критерий | Notion | Trello | Asana | Task Tracker |
|---|---|---|---|---|
| Auto-extract из чатов | ❌ | ❌ | ❌ | ✅ TG + Slack + Email + Meetings |
| Auto-assign owner | ❌ | ❌ | ⚠️ rules-based | ✅ LLM-based + справочник |
| Auto-parse deadline | ❌ | ❌ | ❌ | ✅ "к пятнице" → date |
| Native TG cards | ❌ | ❌ | ❌ | ✅ first-class |
| Дайджесты | ⚠️ email | ⚠️ email | ⚠️ email | ✅ TG (Q2) |
| Recurring tasks | ✅ | ⚠️ basic | ✅ | ✅ (Q2) |
| 2-way Google Tasks | ❌ | ❌ | ❌ | ✅ (Q3) |
| Free | ⚠️ limited | ⚠️ limited | ⚠️ limited | ✅ self-hosted |

## 4.4 Ключевая ценность

> «Я пишу в любом TG-чате 'Алина, сделай X к пятнице' — через 30 секунд Алина получает в TG-DM карточку задачи. Утром я открываю TG и вижу что у меня сегодня 5 задач + 2 просроченных. Я ничего не делал руками.»

## 4.5 Ограничения

- Only Telegram cards (no email, no native iOS app, no web UI на MVP)
- Recipient должен /start-нуть бот (TG limitation)
- LLM зависимость → quality = quality of OpenAI
- Slack / Email / Recurring / Digests — TODO Q2-2026
- Google Tasks 2-way sync — TODO Q3-2026
- Manual `/task` command — TODO Q3-2026

---

# 5. Продуктовые метрики

## 5.1 North Star Metric

**Time-to-Card**: время от события (TG message / Slack post / meeting end) до того, как owner получит actionable TG-карточку.

- **Цель MVP**: ≤ 5 минут (median) для TG flow
- **Цель v1**: ≤ 1 минута (через push events вместо polling, optimization LLM batch)

## 5.2 Метрики качества решения

| ID | Метрика | Что измеряет | Цель |
|---|---|---|---|
| M-Q1 | **Owner-routing accuracy** | % задач без redelegation (LLM правильно выбрал owner) | ≥ 75% |
| M-Q2 | **Deadline parsing accuracy** | % задач, у которых LLM-deadline = ручной выбор admin'a | ≥ 85% |
| M-Q3 | **Task extraction precision** | % извлечённых задач, которые owner подтвердил как валидные (Принять, не Закрыть) | ≥ 80% |
| M-Q4 | **Dedup accuracy** | % дублей правильно отброшенных | ≥ 95% |
| M-Q5 | **Classifier accuracy** | % сообщений, правильно классифицированных task vs chitchat | ≥ 90% |

## 5.3 Метрики пользовательской эффективности

| ID | Метрика | Цель |
|---|---|---|
| M-E1 | **CEO time saved per week** | ≥ 4 часа / неделя на манипуляции с tasks |
| M-E2 | **Tasks completed in time** | ≥ 70% задач закрыты до due_date |
| M-E3 | **Forgotten commitments reduction** | -50% (vs до Task Tracker) |

## 5.4 Метрики использования продукта

| ID | Метрика | Цель |
|---|---|---|
| M-U1 | **Daily active sources** | ≥ 3 источника генерят tasks |
| M-U2 | **Tasks created per day** | 20-50 |
| M-U3 | **Card button engagement** | ≥ 60% карточек получают хоть один button-press |
| M-U4 | **Digest open rate** | ≥ 80% утренних/вечерних в первый час |

## 5.5 Метрики ошибок и сбоев

| ID | Метрика | Цель |
|---|---|---|
| M-F1 | **Pipeline failure rate** | < 3% сообщений вызывают exception |
| M-F2 | **TG card DM failure rate** | < 30% (limit by recipients не /start-нувшими бота) |
| M-F3 | **Listener uptime** | ≥ 99% |
| M-F4 | **GTasks sync conflict rate** (Q3) | < 5% |

---

# 6. Фичи / модули продукта

## 6.1 MVP (must-have, уже работает)

| ID | Фича | Описание | Кому | Проблема | Ценность | Приоритет | Зависимости |
|---|---|---|---|---|---|---|---|
| F-TT-01 | **TG ingest live** | Supabase view + Bot API long-poll, 30s tick | CEO | «Не успеваю 60 чатов» | Auto-tasks из чатов | Must | Supabase + LLM |
| F-TT-02 | **Intent classifier** | LLM classifies task/chitchat/question | Pipeline | «Шум в чатах» | Только tasks обрабатываем | Must | LLM |
| F-TT-03 | **Drafts loop** | LLM-graph: title/desc/owner/date/priority/dedup | Pipeline | «Какая task, кому, когда?» | Auto-extract | Must | LLM |
| F-TT-04 | **Owner LLM-router** | Auto-select owner из team_members по контексту | Owner | «Кому это?» | No manual assignment | Must | team_members + LLM |
| F-TT-05 | **Deadline parser** | LLM parses «к пятнице» / «через 3 дня» в ISO | Owner | «Когда дедлайн?» | Concrete date | Must | LLM |
| F-TT-06 | **Priority inference** | LLM lex-based: low/medium/high/urgent | Owner | «Это срочно?» | Sorted lists | Must | LLM |
| F-TT-07 | **Task dedup** | Topic-prefix + fuzzy title (SeqMatcher ≥0.85) | CEO | «Одна задача 5 раз» | Clean list | Must | LLM + Python |
| F-TT-08 | **TG card lifecycle** | DM-карточка с кнопками, in-place updates | Owner | «Где список и кнопки?» | Single-tap action | Must | TG Bot API + DB |
| F-TT-09 | **Note Taker tasks pickup** | Tasks от Note Taker (через shared DB) → TG cards | Owner | «Задачи из встречи никуда не идут» | Auto-распределение | Must | NT + DB |
| F-TT-10 | **Multi-recipient delivery** | Карточка идёт author + owner + admins (deduped) | All | «Кто видит мою задачу» | Owner-aware DMs | Must | TG Bot |

## 6.2 Должно быть (should-have, Q2-2026)

| ID | Фича | Описание |
|---|---|---|
| F-TT-11 | **Slack ingest** | Socket Mode, ловит сообщения в каналах + @mentions |
| F-TT-12 | **Email ingest** | Gmail label-фильтр через Gmail API |
| F-TT-13 | **Recurring tasks** | `recurring_task_rules` table + cron-loop |
| F-TT-14 | **Morning digest** | TG DM с задачами на сегодня (08:00 local) |
| F-TT-15 | **Evening digest** | TG DM с done за сегодня + план завтра (18:00) |
| F-TT-16 | **Deadline reminders** | За день / в день / при overdue |
| F-TT-17 | **Status update notifications** | Push при смене статуса owner'у/admin'у/subscriber'ам |
| F-TT-18 | **Manual `/task` command** | Создание задачи admin'ом через TG команду |

## 6.3 Может быть (could-have, Q3-Q4 2026)

| ID | Фича | Описание |
|---|---|---|
| F-TT-19 | **Google Tasks 2-way sync** | Push + pull, conflict resolution last-write-wins |
| F-TT-20 | **Subscriptions** | «Подписаться на чужую задачу» — push при изменениях |
| F-TT-21 | **Web admin UI** | CRUD recurring rules, browse all tasks, FTS |
| F-TT-22 | **Inline-edit карточек** | Без separate conversation flow |
| F-TT-23 | **Voice commands в TG** | "Создать задачу..." через voice → speech → text → task |
| F-TT-24 | **Webhook export** | POST на n8n каждый event смены статуса |
| F-TT-25 | **Notion / Airtable native exporters** | Без n8n |

---

# 7. User Stories

## US-TT-1 — TG-сообщение → задача

> Как CEO, я хочу чтобы любое сообщение в TG-чате с глаголом-обязательством («сделать», «отправить», «созвониться») автоматически становилось задачей с правильным owner'ом и сроком, чтобы не пропускать обещания и не вводить руками.

**AC:**
```gherkin
Given новое TG-сообщение "Алина, отправь Марку follow-up до пятницы"
When listener читает Supabase view
Then intent_classifier returns {intent: "task", confidence: >0.8}
And drafts loop генерирует:
  | field | value |
  | title | отправить Марку follow-up |
  | owner_user_id | <Алина TG uid> |
  | due_date | next Friday |
  | priority | medium |
And в `tasks` появляется row с source_kind='telegram'
And TG-карточка отправляется Алине
And TG-карточка дублируется admin'у (CEO)
```

## US-TT-2 — Жизненный цикл карточки

> Как owner, я хочу управлять задачей через кнопки на TG-карточке (Принять / Делегировать / Отложить / Закрыть), чтобы не открывать никаких других UI.

**AC:**
```gherkin
Given Алина получила TG-карточку с task #N
When она нажимает кнопку "Принять"
Then task.status переходит todo → in_progress
And карточка обновляется in-place у Алины и у admin'a
And в task_status_changes появляется audit row

When нажимает "Делегировать" → выбирает Дима
Then task.owner_user_id = Дима uid
And карточка отправляется Диме
And карточка обновляется у предыдущего owner (Алины) и у admin

When нажимает "Закрыть"
Then task.status = done
And карточка обновляется у всех recipients
And если есть Google Tasks linking — gtask.status='completed'
```

## US-TT-3 — Утренний дайджест (Q2)

> Как CEO/owner задач, я хочу в 08:00 получать TG DM со списком задач на сегодня + просроченные, чтобы начать день с фокусом.

**AC:**
```gherkin
Given у user есть active tasks с due_date <= today OR (due_date IS NULL AND priority='urgent')
When system time = 08:00 в TIMEZONE
Then TG DM с заголовком "Доброе утро, [Имя]!"
And список: задачи на сегодня (с emoji status + clickable on full card)
And отдельный блок: ⏰ Просроченные
And footer: "Всего: N active, M overdue"
```

## US-TT-4 — Note Taker tasks pickup

> Как owner, я хочу автоматически получать задачи из встреч (Zoom/Fireflies) в TG, чтобы не открывать meeting summary вручную.

**AC:**
```gherkin
Given Note Taker завершил pipeline для встречи
And в tasks table добавлены rows с source_kind='zoom'/'fireflies'
And source_conversation_id = zoom_id/fireflies_id
When Task Tracker обрабатывает new task rows (через DB watching или polling)
Then для каждой task: TG-карточка идёт owner-у (через team_members.slack_user_id mapping или owner_user_id directly)
And карточка содержит link на Google Doc встречи (через source_permalink)
And admin (CEO) тоже получает карточку
```

## US-TT-5 — Slack ingest (Q2)

> Как CEO, я хочу чтобы любое сообщение в Slack-каналах (где бот добавлен) или @mention автоматически становилось task в Telegram.

**AC:**
```gherkin
Given бот добавлен в Slack-канал #fundraising
And новое сообщение "@Alina отправь updates Mark до пятницы"
When Socket Mode listener получает event
Then context-fetch: parent + last 5 thread replies + last 10 channel messages в окне 5 мин
Then тот же IngestService.classify_and_draft вызывается с source='slack'
And task сохраняется в DB с source_kind='slack'
And TG-карточка идёт Алине (через slack_user_id → telegram_user_id mapping в team_members)
And admin тоже получает
And в Slack бот молчит (никакого ack/emoji/DM)
```

## US-TT-6 — Recurring tasks (Q2)

> Как admin, я хочу настроить regular задачи (например, «отчёт каждый понедельник в 09:00» или «созвон каждые 2 недели»), чтобы не создавать их вручную.

**AC:**
```gherkin
Given добавлено recurring_task_rule:
  | rule_name | template_title | schedule_kind | schedule_args | template_owner |
  | weekly_report | "Подготовить недельный отчёт" | weekly | {"days_of_week":[1]} | Алина |
When system time достигает next_run_at
Then новая Task создаётся из template
And TG-карточка идёт Алине
And next_run_at = next Monday
```

## US-TT-7 — Google Tasks two-way sync (Q3)

> Как CEO (если я power-user GTasks), я хочу чтобы изменения в Task Tracker отражались в Google Tasks и наоборот, чтобы видеть единый список в любом UI.

---

# 8. User Flow

## 8.1 Главный flow

```mermaid
flowchart TD
    A[Кто-то пишет в TG-чате 'Алина, отправь Марку до пятницы'] --> B[Supabase view ловит ≤30s]
    B --> C[Listener читает на следующем tick]
    C --> D[intent_classifier: task?]
    D -->|нет| End1[End: chitchat skip]
    D -->|да| E[Drafts loop]
    E --> F[title_node → 'отправить Марку follow-up']
    F --> G[description_node]
    G --> H[owner_node → Алина из team_members]
    H --> I[date_node → next Friday]
    I --> J[priority_node → medium]
    J --> K[dedup_node → нет дубля]
    K --> L[INSERT в tasks]
    L --> M[Card builder]
    M --> N[TG DM Алине]
    M --> O[TG DM admin]
    N --> P{Алина видит карточку}
    P --> Q[Принять / Делегировать / Закрыть]
```

## 8.2 Flow карточки (button-press)

```mermaid
flowchart TD
    A[Owner получает карточку] --> B{Нажал кнопку}
    B -->|Принять| C[status: todo → in_progress]
    B -->|Делегировать| D[Выбрать нового owner из inline-keyboard]
    B -->|Отложить| E[Inline keyboard: +1 день / +1 неделя]
    B -->|Изменить| F[Conversation-flow в TG: edit title/desc/due]
    B -->|Закрыть| G[status: done]
    B -->|Подписаться| H[Add to task_subscriptions]
    
    C --> I[task_status_changes audit row]
    D --> I
    E --> I
    F --> I
    G --> I
    
    I --> J[edit_message: refresh card у всех recipients]
    J --> K[Push notification subscribers]
```

## 8.3 Flow данных Task Tracker

```mermaid
flowchart LR
    TGSrc[TG: Supabase view] -->|poll 30s| TI[TG Ingest]
    SlackSrc[Slack channels TODO] -.->|Socket Mode| SI[Slack Ingest]
    EmailSrc[Email Inbox TODO] -.->|IMAP poll| EI[Email Ingest]
    NTSrc[Note Taker shared DB] --> NTI[NT-tasks Reader]
    Manual[/task command TODO] -.-> MI[Manual Ingest]
    Recurring[Recurring scheduler TODO] -.-> RI[Recurring Trigger]

    TI --> CL[LLM Classifier]
    SI -.-> CL
    EI -.-> CL

    CL -->|task| Drafts[Drafts loop]
    NTI --> Drafts
    MI -.-> Drafts
    RI -.-> Drafts
    CL -->|chitchat| Skip[Skip]

    Drafts --> Tm[(team_members)]
    Drafts --> TaskDB[(tasks DB)]

    Drafts --> Card[TG Card Builder]
    Card --> Recipients{Recipients}
    Recipients -->|Owner| TG1[TG DM owner]
    Recipients -->|Admin| TG2[TG DM admins]
    Recipients -->|Subs TODO| TG3[TG DM subs]

    TaskDB --> GTSync[GTasks Sync TODO]
    TaskDB --> WhExp[Webhook Export TODO]
    TaskDB --> DigSrv[Digest Service TODO]
```

---

# 9. BDD Use Cases

## Use Case Map

| UC ID | Название | Фича | User Story | Приоритет |
|---|---|---|---|---|
| UC-TT-01 | TG-сообщение → task | F-TT-01..04, 07, 08, 10 | US-TT-1 | Must |
| UC-TT-02 | Card lifecycle (buttons) | F-TT-08 | US-TT-2 | Must |
| UC-TT-03 | Slack-сообщение → task (TODO) | F-TT-11 | US-TT-5 | Should Q2 |
| UC-TT-04 | Email-сообщение → task (TODO) | F-TT-12 | (no full US yet) | Should Q2 |
| UC-TT-05 | Note Taker tasks pickup | F-TT-09 | US-TT-4 | Must |
| UC-TT-06 | Morning digest (TODO) | F-TT-14 | US-TT-3 | Should Q2 |
| UC-TT-07 | Evening digest (TODO) | F-TT-15 | (US-TT-3 mirror) | Should Q2 |
| UC-TT-08 | Deadline reminders (TODO) | F-TT-16 | (no full US yet) | Should Q2 |
| UC-TT-09 | Recurring tasks (TODO) | F-TT-13 | US-TT-6 | Should Q2 |
| UC-TT-10 | Google Tasks 2-way sync (TODO) | F-TT-19 | US-TT-7 | Could Q3 |
| UC-TT-11 | Manual `/task` command (TODO) | F-TT-18 | (no full US yet) | Should Q3 |
| UC-TT-12 | Subscriptions (TODO) | F-TT-20 | (no full US yet) | Could Q3 |

## Пример full Gherkin: UC-TT-01

```gherkin
Feature: UC-TT-01 — Telegram message → task
  Implements: US-TT-1
  Covers: FR-TT-1.1, FR-TT-2.1, FR-TT-3.1..3.5, FR-TT-4.1, FR-TT-5.1, FR-TT-6.1
  Tested by: T-TT-001..010

  Background:
    Given listener запущен с VIEW_REALTIME_ENABLED=true
    And в team_members есть row real_name="Алина Колпакова" telegram_user_id=<int>
    And у Алины есть /start-нутый бот

  Scenario: Простая задача с явным owner и deadline
    Given в TG-чате с message_id=12345 пришло "Алина, отправь Марку follow-up до пятницы"
    When listener читает Supabase view
    Then intent classifier returns {intent: "task", confidence: >0.8}
    And drafts loop создаёт ActionDraft с:
      | field | value |
      | title | отправить Марку follow-up |
      | owner_user_id | <Алина TG uid> |
      | due_date | next Friday |
      | priority | medium |
    And в `tasks` появляется row с source_kind='telegram'
    And TG-карточка отправляется Алине в DM
    And TG-карточка отправляется admin (CEO)

  Scenario: Сообщение классифицировано как chitchat
    Given сообщение "ага понял спасибо"
    When intent classifier returns {intent: "chitchat"}
    Then drafts loop НЕ запускается
    And в `tasks` ничего не записывается
    And `processed_telegram_messages` помечает (chat_id, message_id) как seen

  Scenario: Owner не найден в team_members
    Given сообщение "Кто-то проверьте контракт"
    When owner_node не находит match (final_uid=None)
    Then fallback owner = admin (CEO)
    And карточка идёт только admin'у

  Scenario: Recipient не /start-нул бота
    Given owner = "Дима Дроздов" с telegram_user_id=<int>
    But Дима не /start-нул бота (chat not found)
    When TG bot пытается sendMessage
    Then API возвращает 400 "chat not found"
    And telegram_card_dm_failed логируется на info уровне
    And карточка всё равно идёт admin'у
    And task сохранена в DB

  Scenario: Дубликат
    Given в DB уже есть task #2649 "написать Йохану по BYD"
    And новое сообщение "Глянь выходы на BYD"
    When dedup_node сравнивает
    Then результат: duplicate_of=2649
    And новый task НЕ создаётся
    And telegram_prepare_drafts_skipped_duplicate логируется
```

## Пример: UC-TT-02

```gherkin
Feature: UC-TT-02 — Card lifecycle
  Implements: US-TT-2
  Covers: FR-TT-7.1..7.4

  Scenario: Принять
    Given у Алины открыта TG-карточка task #N (status: todo)
    When Алина нажимает "✅ Принять"
    Then callback_query handler вызывается
    And task.status = in_progress
    And task_status_changes audit row: old=todo, new=in_progress, by=Алина TG uid
    And edit_message обновляет card у Алины с новым status emoji
    And edit_message обновляет card у admin тоже

  Scenario: Делегировать
    Given owner = Алина
    When Алина нажимает "➡️ Делегировать"
    Then показывается inline keyboard со списком team_members
    When выбрала "Дима Дроздов"
    Then task.owner_user_id = Дима uid
    And TG card отправляется Диме
    And carcточка обновляется у Алины и у admin (новый owner показан)
    And task_status_changes audit row

  Scenario: Закрыть
    Given task #N в любом status кроме done
    When нажимает "❌ Закрыть"
    Then optional reason text запрашивается
    And task.status = done, task.closed_at = now
    And карточки обновляются у всех recipients
    And если есть google_task_id — gtask.status='completed' (Q3)
```

---

# 10. Functional Requirements Register (Task Tracker)

## Категория 1 — Source ingestion

| ID | Требование | Приоритет | UC | Test |
|---|---|---|---|---|
| FR-TT-1.1 | Чтение TG через Supabase view (period: `VIEW_POLL_INTERVAL_SECONDS`, default 30s) | Must | UC-TT-01 | T-TT-001 |
| FR-TT-1.2 | Получать до `VIEW_POLL_BATCH_SIZE` (default 500) последних сообщений | Must | UC-TT-01 | T-TT-001 |
| FR-TT-1.3 | ✅ Slack Socket Mode + ловить `message.channels`, `message.groups`, `message.im`, `message.mpim`, `app_mention` (DELIVERED 2026-05-13, FR-CR-05-162) | Should | UC-TT-03 | T-TT-020 |
| FR-TT-1.4 | ✅ Slack: thread context (parent + last 5 replies) + main channel context (last 10 messages в окне 5 мин) (DELIVERED 2026-05-13, FR-CR-05-162) | Should | UC-TT-03 | T-TT-021 |
| FR-TT-1.3a | ✅ Slack ingest = TG feature-parity: multi-task per message, intra+cross dedup, owner resolution chain, TG-card delivery via `post_initial_card(for_slack_ingest=True)` (DELIVERED 2026-05-13, FR-CR-05-162) | Must | UC-TT-03 | tests/test_slack_ingest.py |
| FR-TT-1.5 | (TODO) Email через Gmail label-фильтр | Should | UC-TT-04 | T-TT-030 |
| FR-TT-1.6 | Auto-pickup tasks от Note Taker (через DB, source_kind ∈ {zoom, fireflies, gmeet, manual}) | Must | UC-TT-05 | T-TT-040 |
| FR-TT-1.7 | (TODO) Manual `/task <text>` команда от admin в TG | Should | UC-TT-11 | T-TT-050 |
| FR-TT-1.8 | (TODO) Recurring scheduler триггерит задачи по cron | Should | UC-TT-09 | T-TT-060 |

## Категория 2 — Classification

| ID | Требование | Приоритет | UC | Test |
|---|---|---|---|---|
| FR-TT-2.1 | LLM-classifier returns intent ∈ {task, chitchat, question, status_update} с confidence | Must | UC-TT-01 | T-TT-002 |
| FR-TT-2.2 | Только intent="task" идёт в drafts loop | Must | UC-TT-01 | T-TT-002 |
| FR-TT-2.3 | Pre-startup messages (sent_at < listener startup) → skip (cutoff FR-CR-05-51) | Must | UC-TT-01 | T-TT-003 |

## Категория 3 — Drafts pipeline

| ID | Требование | Приоритет | UC | Test |
|---|---|---|---|---|
| FR-TT-3.1 | title_node: короткое название ≤60 chars | Must | UC-TT-01 | T-TT-004 |
| FR-TT-3.2 | description_node: подробности ≤500 chars | Must | UC-TT-01 | T-TT-004 |
| FR-TT-3.3 | owner_node: auto-assign из team_members через LLM | Must | UC-TT-01 | T-TT-005 |
| FR-TT-3.4 | date_node: parse "к пятнице"/"через 3 дня" → ISO date с reasoning | Must | UC-TT-01 | T-TT-006 |
| FR-TT-3.5 | priority_node: low/medium/high/urgent | Must | UC-TT-01 | T-TT-007 |

## Категория 4 — Dedup

| ID | Требование | Приоритет | UC | Test |
|---|---|---|---|---|
| FR-TT-4.1 | Topic-prefix exact match (case-insensitive, NFKD-folded) → дубль | Must | UC-TT-01 | T-TT-008 |
| FR-TT-4.2 | Title fuzzy через SequenceMatcher ≥0.85 → дубль | Must | UC-TT-01 | T-TT-008 |
| FR-TT-4.3 | Owner overlap → strict дубль | Should | UC-TT-01 | T-TT-008 |

## Категория 5 — Persistence

| ID | Требование | Приоритет | UC | Test |
|---|---|---|---|---|
| FR-TT-5.1 | Сохранение task в `tasks` table со всеми полями (source_kind, source_*, title, desc, owner, due, priority, status='todo') | Must | UC-TT-01 | T-TT-009 |
| FR-TT-5.2 | `processed_<source>_messages` table для dedup сообщений | Must | UC-TT-01 | T-TT-009 |
| FR-TT-5.3 | Soft-delete через `deleted_at` (для audit trail) | Must | UC-TT-02 | T-TT-010 |

## Категория 6 — Distribution (TG cards)

| ID | Требование | Приоритет | UC | Test |
|---|---|---|---|---|
| FR-TT-6.1 | Карточка содержит: title, owner, due, priority, status, description preview, source link | Must | UC-TT-02 | T-TT-011 |
| FR-TT-6.2 | Recipients = author + owner (через team_members mapping) + admins | Must | UC-TT-02 | T-TT-011 |
| FR-TT-6.3 | Длинные cards >4096 chars splitting на paragraph boundaries | Must | UC-TT-02 | T-TT-012 |
| FR-TT-6.4 | TG card DM failure (`chat not found`) — log info, продолжаем для остальных recipients | Must | UC-TT-02 | T-TT-013 |

## Категория 7 — Card lifecycle

| ID | Требование | Приоритет | UC | Test |
|---|---|---|---|---|
| FR-TT-7.1 | Кнопки: Принять / Делегировать / Отложить / Изменить / Закрыть / Подписаться / Refresh | Must | UC-TT-02 | T-TT-014 |
| FR-TT-7.2 | Каждое нажатие update'ит card in-place у всех recipients (edit_message) | Must | UC-TT-02 | T-TT-014 |
| FR-TT-7.3 | Audit row в `task_status_changes` с old_status, new_status, changed_by, reason | Should | UC-TT-02 | T-TT-015 |
| FR-TT-7.4 | Delegate → новый owner получает карточку, старый owner сохраняется в audit | Must | UC-TT-02 | T-TT-016 |

## Категория 8 — Digests (TODO)

| ID | Требование | Приоритет | UC |
|---|---|---|---|
| FR-TT-8.1 | Morning digest в `MORNING_DIGEST_HOUR_LOCAL` (default 8:00) для всех owner с active tasks | Should | UC-TT-06 |
| FR-TT-8.2 | Evening digest в `EVENING_DIGEST_HOUR_LOCAL` (default 18:00) | Should | UC-TT-07 |
| FR-TT-8.3 | Skip if no active tasks (no empty digest) | Should | UC-TT-06 |
| FR-TT-8.4 | Часовой пояс через `TIMEZONE` env (default Europe/London) | Should | UC-TT-06 |

## Категория 9 — Reminders (TODO)

| ID | Требование | Приоритет | UC |
|---|---|---|---|
| FR-TT-9.1 | За день до дедлайна (07:00 утром) → push owner + admin | Should | UC-TT-08 |
| FR-TT-9.2 | В день дедлайна (09:00 + 16:00) | Should | UC-TT-08 |
| FR-TT-9.3 | При просрочке (next morning 09:00, потом раз в 3 дня) | Should | UC-TT-08 |

## Категория 10 — Recurring tasks (TODO)

| ID | Требование | Приоритет | UC |
|---|---|---|---|
| FR-TT-10.1 | `recurring_task_rules` table с template_*, schedule_kind, schedule_args, next_run_at, enabled | Should | UC-TT-09 |
| FR-TT-10.2 | Cron внутри listener каждые 5 мин: SELECT enabled AND next_run_at <= NOW(), spawn Task, update next_run_at | Should | UC-TT-09 |
| FR-TT-10.3 | schedule_kind ∈ {daily, weekly, monthly, cron} | Should | UC-TT-09 |

## Категория 11 — Google Tasks 2-way sync (TODO Q3)

| ID | Требование | Приоритет | UC |
|---|---|---|---|
| FR-TT-11.1 | Push: при INSERT/UPDATE Task → POST/PATCH в GTasks API, mapping fields | Could | UC-TT-10 |
| FR-TT-11.2 | Pull: каждые 60s tasks.list, sync changes back | Could | UC-TT-10 |
| FR-TT-11.3 | Conflict resolution: last-write-wins by updated_at | Could | UC-TT-10 |
| FR-TT-11.4 | Linking через `tasks.google_task_id` | Could | UC-TT-10 |

## Категория 12 — Subscriptions (TODO Q3)

| ID | Требование | Приоритет | UC |
|---|---|---|---|
| FR-TT-12.1 | `task_subscriptions` table | Could | UC-TT-12 |
| FR-TT-12.2 | Push notification subscriber'ам при смене статуса | Could | UC-TT-12 |

## Категория 13 — Audit log

| ID | Требование | Приоритет | UC |
|---|---|---|---|
| FR-TT-13.1 | `task_status_changes` table: task_id, old_status, new_status, changed_by_user_id, changed_at, reason | Should | UC-TT-02 |

---

# 11. Non-Functional Requirements Register (Task Tracker)

| ID | Категория | Требование | Цель |
|---|---|---|---|
| NFR-TT-P.1 | Performance | Time-from-message to TG card ≤ 5 min (median) | ≤ 5 min |
| NFR-TT-P.2 | Performance | TG card button-press response ≤ 2s (perceived) | ≤ 2s |
| NFR-TT-P.3 | Performance | Listener tick handles 500 messages в batch без timeout (~30 минут на batch при 5 task'ов в среднем) | ≤ 30 min/batch |
| NFR-TT-R.1 | Reliability | TG long-poll auto-reconnect at network failures | Enforced |
| NFR-TT-R.2 | Reliability | Listener uptime ≥ 99% | ≥ 99% |
| NFR-TT-S.1 | Security | TG bot token не логируется и не экспонируется | Enforced |
| NFR-TT-S.2 | Security | Slack token + scopes минимально необходимые (только history + chat:write) | Enforced |
| NFR-TT-O.1 | Observability | Каждое intent_classify + drafts loop логируется с message_id и LLM reasoning | Enforced |
| NFR-TT-O.2 | Observability | Структурированные events: `telegram_prepare_drafts_loop_start/done`, `owner_node_result`, `date_node_result` | Enforced |
| NFR-TT-C.1 | Cost | Per-message LLM ≤ $0.10 (typical: 5-7 LLM calls × $0.01-$0.02) | ≤ $0.10/msg |
| NFR-TT-C.2 | Cost | Daily LLM cost ≤ $50 для 50 tasks/day | ≤ $50/day |
| NFR-TT-U.1 | Usability | Кнопки на карточке универсально-понятны без обучения (emoji + Russian labels) | Enforced |
| NFR-TT-U.2 | Usability | Card обновляется in-place при button-press (нет «новой» карточки в чате) | Enforced |
| NFR-TT-I.1 | Integration | Google Tasks 2-way sync разрешает конфликты last-write-wins (Q3) | TODO |
| NFR-TT-I.2 | Integration | Slack scope minimal: bot token не должен иметь deletion / admin scopes | Enforced |
| NFR-TT-D.1 | Data | Soft-delete (deleted_at IS NOT NULL) вместо hard-delete | Enforced |
| NFR-TT-D.2 | Data | Audit trail каждого изменения статуса | Enforced (Q2) |

---

# 12. Architecture (Solution Design)

## 12.1 Architecture Summary

Task Tracker — Python 3.11 service в Docker. Source-agnostic ingestion, common LLM-graph drafts loop, output ТОЛЬКО в Telegram (на MVP). Состояние хранится в Postgres rows.

LLM-стек: OpenAI (GPT-5.4 для drafts loop nodes, GPT-4o для tasks model).

Деплой: GCP Compute Engine VM (`human-1`), Docker контейнер `slack-task-tg-listener` (общий с Note Taker сейчас, разделится в Q3).

## 12.2 Project Structure (Task Tracker scope)

```
/manager
├── app/
│   ├── config.py
│   ├── db/
│   ├── models/
│   │   ├── task.py                  # Task, TaskSourceKind enum
│   │   ├── team.py                  # team_members (shared)
│   │   ├── recurring_task_rule.py   # TODO Q2
│   │   ├── task_status_change.py    # TODO Q2
│   │   └── task_subscription.py     # TODO Q3
│   ├── telegram_bot/
│   │   ├── listener.py              # main: TG long-poll + Supabase view poll
│   │   ├── cards.py                 # post_initial_card, refresh_card, keyboards
│   │   ├── handlers.py              # callback_query handlers (button presses)
│   │   ├── sender.py                # TelegramSender wrapper
│   │   ├── morning_cards.py         # TODO Q2
│   │   └── evening_status.py        # TODO Q2
│   ├── slack_ingest/                # TODO Q2: Slack Socket-Mode ingest
│   │   ├── listener.py
│   │   └── handler.py
│   ├── email_ingest/                # TODO Q2: Gmail IMAP ingest
│   ├── intent/
│   │   ├── classifier.py            # intent classify
│   │   └── llm_backends.py          # OpenAIBackend
│   ├── telegram_ingest/
│   │   └── service.py               # TelegramIngestService.prepare_drafts
│   ├── services/
│   │   ├── ingest.py                # generic ingest helpers
│   │   ├── digest.py                # TODO Q2: digest generators
│   │   ├── recurring_scheduler.py   # TODO Q2
│   │   ├── deadline_reminders.py    # TODO Q2
│   │   ├── google_tasks_sync.py     # TODO Q3
│   │   └── team_members.py          # справочник helpers
│   └── orchestrator/
├── ops/
│   ├── telegram_listener.py         # main entrypoint
│   ├── slack_listener.py            # TODO Q2 entrypoint
│   ├── send_digest.py               # cron-trigger digests
│   └── ...
├── alembic/
└── docs/specs/task_tracker/         # this spec
```

## 12.3 Client Layer

Task Tracker имеет **только Telegram UI** (на MVP). Все взаимодействия через TG bot DMs.

### Карточки (screens)

| Screen ID | Назначение | Когда показывается | UX элементы |
|---|---|---|---|
| **S-Card-Initial** | Новая task carcточка после извлечения | Сразу после INSERT в `tasks` | Title, owner, due, priority, status, desc preview, source link, кнопки |
| **S-Card-Refresh** | Обновлённая карточка после button press | После любого изменения статуса/owner'а | То же что S-Card-Initial с актуальным state |
| **S-MorningDigest** (Q2) | Утренний дайджест (08:00) | Раз в день admin'у и owner'ам с active tasks | Список задач на сегодня + просроченные |
| **S-EveningDigest** (Q2) | Вечерний дайджест (18:00) | Раз в день | Done сегодня + план завтра |
| **S-DeadlineReminder** (Q2) | Push-нотификация о дедлайне | За день / в день / при overdue | Карточка задачи + кнопки |
| **S-DelegateKeyboard** | Inline-keyboard со списком команды | После нажатия Делегировать | Список team_members |
| **S-PostponeKeyboard** | Inline-keyboard для переноса дедлайна | После нажатия Отложить | "+1 день / +1 неделя / другое" |
| **S-EditConversation** | Conversation-flow для редактирования task | После нажатия Изменить | Серия TG messages: "Новый title?" → "Новый desc?" → ... |

### Состояния карточки

| State | Что показывается |
|---|---|
| **Default** | Полная карточка с кнопками для роли viewer'a (owner / admin / bystander) |
| **Loading** | После button press — "..." пока обработка |
| **Updated** | После обработки — message edit с новым state |
| **Error** | "❌ Не удалось обновить, попробуйте ещё раз" |

### Кнопки и роли

| Кнопка | Кто видит | Действие |
|---|---|---|
| ✅ Принять | Owner | status: TODO → IN_PROGRESS |
| ➡️ Делегировать | Owner | Открыть inline-keyboard со списком team_members |
| ⏰ Отложить | Owner | Inline-keyboard: переnos due_date |
| ✏️ Изменить | Owner / Admin | Conversation-flow для edit |
| ❌ Закрыть | Owner / Admin | status → DONE, optional reason |
| 👀 Подписаться (Q3) | Любой viewer | Получает notifications о смене статуса |
| 🔄 Refresh | Любой | Перерисовать карточку |

## 12.4 Service Layer

| Service | Назначение | UC | API/Methods | Dependencies |
|---|---|---|---|---|
| **TelegramIngestService** | TG message → drafts → cards | UC-TT-01 | `prepare_drafts(session, message, classification)` | LLM, team_members, dedup, TelegramSender |
| **SlackIngestService** (TODO) | Slack message → drafts → cards | UC-TT-03 | `handle_message(event)` | slack_bolt, IngestService, TG cards |
| **EmailIngestService** (TODO) | Email → drafts → cards | UC-TT-04 | `pull_inbox()` | Gmail API, IngestService |
| **IntentClassifier** | classify message: task / chitchat / question / status | UC-TT-01 | `classify(message)` | LLM |
| **DraftsGraph** | LLM-graph: title + desc + owner + date + priority + dedup | UC-TT-01 | `run(message, context)` | LLM nodes |
| **TaskCardBuilder** | Build TG card text + keyboard | UC-TT-02 | `post_initial_card(...)`, `refresh_card(...)` | TG Bot API, team_members |
| **CallbackHandler** | Process button-press callback_query | UC-TT-02 | `handle_callback_query(...)` | DB, card refresh |
| **DigestService** (TODO) | Утренние/вечерние дайджесты | UC-TT-06, UC-TT-07 | `send(session, kind)` | tasks query, TG sender |
| **DeadlineReminderService** (TODO) | Push reminders | UC-TT-08 | `tick()` | tasks query, TG sender |
| **RecurringScheduler** (TODO) | Cron-loop для recurring rules | UC-TT-09 | `tick()` | recurring_task_rules table |
| **GoogleTasksSync** (TODO Q3) | 2-way sync с GTasks | UC-TT-10 | `push_changes()`, `pull_changes()` | Google Tasks API |

## 12.5 AI Service Layer

| AI Service | Назначение | LLM call | Model | Prompt file |
|---|---|---|---|---|
| **IntentClassifier** | message → intent {task, chitchat, question, status_update} | chat.completions | openai_model (gpt-4o) | `docs/prompts/intent_classifier.md` |
| **TitleNode** | message → short title ≤60 chars | chat.completions | openai_model | `docs/prompts/title_node.md` |
| **DescriptionNode** | message → desc ≤500 chars | chat.completions | openai_model | `docs/prompts/description_node.md` |
| **OwnerNode** | message + team_members → owner_uid | chat.completions | openai_model | `docs/prompts/owner_node.md` |
| **DateNode** | message → ISO date с reasoning | chat.completions | openai_date_model (gpt-5.4) | `docs/prompts/date_node.md` |
| **PriorityNode** | message → low/medium/high/urgent | chat.completions | openai_model | `docs/prompts/priority_node.md` |
| **DedupNode** | new draft + existing tasks → dedup decision | chat.completions | openai_dedup_model (gpt-5.4) | `docs/prompts/dedup_node.md` |

### Prompt Contract template

См. SPEC_NOTE_TAKER_v0.1.md секция 12.5 (общий шаблон).

## 12.6 Data Layer

```mermaid
erDiagram
  TEAM_MEMBERS ||--o{ TASKS : "owns"
  TEAM_MEMBERS ||--o{ TASKS : "created_by"
  TASKS ||--o{ TASK_STATUS_CHANGES : "audit"
  TASKS ||--o{ TASK_SUBSCRIPTIONS : "subscribed_by"
  TEAM_MEMBERS ||--o{ TASK_SUBSCRIPTIONS : "subscriber"
  RECURRING_TASK_RULES ||--o{ TASKS : "spawns"
  PROCESSED_TELEGRAM_MESSAGES }o--|| TASKS : "may_link_to"

  TEAM_MEMBERS {
    int id PK
    string real_name
    bigint telegram_user_id
    string telegram_username
    string slack_user_id
    string role
    string notes
  }

  TASKS {
    int id PK
    string source_kind "telegram|slack|zoom|fireflies|email|manual|recurring|google_tasks"
    string source_conversation_id
    string source_message_id
    string source_permalink
    string title
    text description
    bigint owner_user_id
    string owner_display_name
    string priority "low|medium|high|urgent"
    string status "todo|in_progress|blocked|done|cancelled"
    date due_date
    time due_time
    string google_task_id "Q3 sync"
    timestamptz created_at
    timestamptz updated_at
    timestamptz deleted_at "soft delete"
    string card_channel
    string card_ts
    jsonb extra "telegram_cards: [{chat_id, message_id}, ...]"
  }

  TASK_STATUS_CHANGES {
    int id PK
    int task_id FK
    string old_status
    string new_status
    bigint changed_by_user_id
    timestamptz changed_at
    text reason
  }

  TASK_SUBSCRIPTIONS {
    int task_id FK
    bigint subscriber_user_id FK
    timestamptz subscribed_at
  }

  RECURRING_TASK_RULES {
    int id PK
    string rule_name
    string template_title
    text template_description
    bigint template_owner_user_id
    string template_priority
    string schedule_kind "daily|weekly|monthly|cron"
    jsonb schedule_args
    timestamptz next_run_at
    bool enabled
    timestamptz created_at
    timestamptz updated_at
  }

  PROCESSED_TELEGRAM_MESSAGES {
    bigint chat_id PK
    bigint message_id PK
    timestamptz processed_at
    int task_id FK
  }

  PROCESSED_SLACK_MESSAGES {
    string channel_id PK
    string message_ts PK
    timestamptz processed_at
    int task_id FK
  }

  PROCESSED_EMAIL_MESSAGES {
    string message_id PK
    timestamptz processed_at
    int task_id FK
  }
```

### Data Flow

```mermaid
flowchart LR
    User[User]
    TGAPI[TG Bot API]
    DB[(Postgres)]
    LLM[OpenAI GPT]
    GTAPI[Google Tasks API Q3]

    User --> TGAPI
    TGAPI -->|callback_query| Listener
    Listener --> DB
    Listener --> LLM
    LLM --> Listener
    Listener --> TGAPI

    SupabaseView[(Supabase view)] -->|poll| Listener

    DB --> Digest[Digest Service Q2]
    Digest --> TGAPI

    DB --> Reminder[Reminder Service Q2]
    Reminder --> TGAPI

    DB --> GTSync[GTasks Sync Q3]
    GTSync --> GTAPI
    GTAPI --> GTSync
    GTSync --> DB
```

## 12.7 Infrastructure Layer

То же самое, что Note Taker (один контейнер на оба agent'a сейчас, раздельные в Q3).

| Component | Implementation |
|---|---|
| Runtime | Python 3.11 в Docker |
| Hosting | GCP Compute Engine `human-1` |
| Database | Postgres 16 в Docker |
| Queue | Postgres rows + polling |
| Secrets | env-file mode 600 |
| CI/CD | Manual git+docker |
| Monitoring | structlog → docker logs |
| Backups | None (TODO pg_dump cron) |
| Security | Internal network + GCP firewall |
| Environments | Production only |
| Scaling | Vertical |

## 12.8 TDD Strategy

```
       /\
      /e2e\           5%   E2E с реальным TG bot test account
     /------\
    /  int   \       30%  Pipeline integration with DB + LLM mocks
   /----------\
  /    unit    \     65%  Pure functions (dedup logic, owner-mapping)
 /--------------\
```

| Test ID | Type | Component | Scenario | Covers FR | Covers UC |
|---|---|---|---|---|---|
| T-TT-001 | int | TelegramIngestService | message → task in DB | FR-TT-1.1, 5.1 | UC-TT-01 |
| T-TT-002 | unit | IntentClassifier | "Алина, сделай X" → task | FR-TT-2.1 | UC-TT-01 |
| T-TT-003 | unit | listener | pre-startup cutoff | FR-TT-2.3 | UC-TT-01 |
| T-TT-005 | int | OwnerNode | resolve via team_members | FR-TT-3.3 | UC-TT-01 |
| T-TT-006 | int | DateNode | "к пятнице" → ISO | FR-TT-3.4 | UC-TT-01 |
| T-TT-008 | unit | DedupNode | fuzzy match ≥0.85 | FR-TT-4.1, 4.2 | UC-TT-01 |
| T-TT-009 | int | tasks INSERT | full row saved | FR-TT-5.1 | UC-TT-01 |
| T-TT-011 | int | post_initial_card | card к owner + admin | FR-TT-6.1, 6.2 | UC-TT-02 |
| T-TT-013 | unit | post_initial_card | recipient без /start fail-safe | FR-TT-6.4 | UC-TT-02 |
| T-TT-014 | int | callback_handler | "Принять" → status change + edit_message | FR-TT-7.1, 7.2 | UC-TT-02 |
| T-TT-015 | int | task_status_changes | audit row создаётся | FR-TT-7.3 | UC-TT-02 |
| T-TT-020 | int | SlackIngestService (Q2) | message → task in DB | FR-TT-1.3 | UC-TT-03 |
| T-TT-021 | unit | Slack context-fetch | thread + 5 replies + 10 channel msgs | FR-TT-1.4 | UC-TT-03 |
| T-TT-040 | int | NT-tasks pickup | Note Taker INSERT → TG card | FR-TT-1.6 | UC-TT-05 |

## 12.9 Architecture Diagrams

### High-Level

```mermaid
flowchart TD
    User[CEO/Owner]
    TGBot[Telegram Bot]
    Listener[Listener container]
    LLM[OpenAI GPT]
    DB[(Postgres)]
    SupabaseView[(Supabase TG view)]
    SlackAPI[Slack API Q2]
    GmailAPI[Gmail API Q2]
    GTasksAPI[Google Tasks API Q3]

    User <--> TGBot
    TGBot <--> Listener
    Listener --> SupabaseView
    Listener --> LLM
    Listener --> DB
    Listener -.-> SlackAPI
    Listener -.-> GmailAPI
    Listener -.-> GTasksAPI
```

### Client-to-Service Flow (TG message → task → card)

```mermaid
sequenceDiagram
    participant U as User (TG chat)
    participant SV as Supabase view
    participant L as Listener
    participant IS as IngestService
    participant CL as Classifier (LLM)
    participant DG as DraftsGraph (LLM)
    participant DB as Postgres
    participant TG as TG Bot API
    participant Owner as Owner (TG DM)

    U->>SV: post message
    SV->>L: poll batch (every 30s)
    L->>IS: prepare_drafts(message)
    IS->>CL: classify intent
    CL-->>IS: {intent: "task", conf: 0.92}
    IS->>DG: run drafts loop
    DG->>DG: title_node + desc + owner + date + priority + dedup
    DG-->>IS: ActionDraft
    IS->>DB: INSERT tasks row
    IS->>TG: sendMessage(owner_uid, card)
    TG-->>Owner: card
    IS->>TG: sendMessage(admin_uid, card)
```

### Service-to-AI-Service Flow (drafts loop)

```mermaid
sequenceDiagram
    participant DG as DraftsGraph
    participant LLM as OpenAI Chat
    participant DB as Postgres
    participant TM as team_members table

    DG->>LLM: title_node prompt
    LLM-->>DG: title
    DG->>LLM: description_node prompt
    LLM-->>DG: description
    DG->>TM: SELECT real_name, telegram_user_id, role, notes
    TM-->>DG: team list
    DG->>LLM: owner_node prompt (text + team)
    LLM-->>DG: {owner_uid, llm_picked_name, reasoning}
    DG->>DG: post-LLM check: owner_uid in team?
    alt Found
        DG->>DG: final_uid = owner_uid
    else Not found
        DG->>DG: final_uid = admin (fallback)
    end
    DG->>LLM: date_node prompt
    LLM-->>DG: {iso_date, reasoning}
    DG->>LLM: priority_node prompt
    LLM-->>DG: priority
    DG->>DB: SELECT existing tasks (last 30 days)
    DG->>LLM: dedup_node prompt
    LLM-->>DG: {is_duplicate, duplicate_of}
```

### Card Lifecycle Flow

```mermaid
sequenceDiagram
    participant U as Owner
    participant TG as TG Bot
    participant H as CallbackHandler
    participant DB as Postgres
    participant TGAdmin as Admin (TG)

    U->>TG: tap "✅ Принять"
    TG->>H: callback_query
    H->>DB: UPDATE task SET status='in_progress'
    H->>DB: INSERT task_status_changes
    H->>TG: editMessageText (owner card)
    TG->>U: refreshed card
    H->>TG: editMessageText (admin card)
    TG->>TGAdmin: refreshed card
```

### Data Flow Diagram

```mermaid
flowchart LR
    User[CEO/Owner]
    TGAPI[TG Bot API]
    DB[(Postgres)]
    LLM[OpenAI GPT]
    SupabaseView[(Supabase view)]
    SlackAPI[Slack API Q2]
    GmailAPI[Gmail API Q2]
    GTasksAPI[Google Tasks API Q3]

    User --> TGAPI
    TGAPI --> Listener
    Listener --> SupabaseView
    Listener -.-> SlackAPI
    Listener -.-> GmailAPI
    Listener --> LLM
    Listener --> DB
    Listener --> TGAPI

    DB --> Digest[Digest Service Q2]
    Digest --> TGAPI

    DB --> Reminder[Reminder Service Q2]
    Reminder --> TGAPI

    DB --> GTSync[GTasks Sync Q3]
    GTSync --> GTasksAPI
    GTasksAPI --> GTSync
    GTSync --> DB
```

## 12.10 Architecture Decision Records (ADR)

### ADR-TT-001: Single LLM-graph для drafts loop

- **Контекст:** нужно извлечь title, desc, owner, date, priority из одного сообщения
- **Решение:** LLM-graph с N nodes (title_node, owner_node, date_node, etc), каждый отдельный LLM call
- **Альтернативы:** один LLM call с structured output для всех полей
- **Почему:** изоляция ошибок (если date_node fails, остальное работает); per-node prompt можно тюнить отдельно; reasoning логируется per node
- **Последствия:** ~5-7 LLM calls на сообщение vs 1; стоит больше, но качество и observability выше

### ADR-TT-002: Postgres polling vs Supabase Realtime

- **Контекст:** TG-сообщения приходят в Supabase view. Нужно их забирать
- **Решение:** Polling каждые 30s
- **Альтернативы:** Supabase Realtime (PG NOTIFY) — push events
- **Почему:** простота setup'a, supabase-py легко работает; latency 30s acceptable
- **Риски:** при потоке >500 msg/30s окно лагает → Mitigation: bump VIEW_POLL_BATCH_SIZE до 2000

### ADR-TT-003: Soft delete vs hard delete

- **Контекст:** task закрыта (status=done) или отменена (cancelled)
- **Решение:** Soft delete через `deleted_at IS NOT NULL`, никогда DELETE
- **Альтернативы:** Hard delete с audit table
- **Почему:** Audit trail нужен для CEO «что я закрыл вчера», dedup поверх deleted задач, GTasks sync conflict resolution
- **Последствия:** tasks table растёт; нужен periodic compaction TODO

### ADR-TT-004: Telegram-only output (no Slack reply)

- **Контекст:** Source может быть Slack, но output — только TG
- **Решение:** Slack ingest НЕ ack'ает сообщения emoji'ями, не шлёт DM в Slack, никаких channel posts
- **Альтернативы:** Two-way Slack acks
- **Почему:** Operator-pinned: «не выводить ни в диалогах ни в самом слаке, только в телеграме»
- **Риски:** Slack sender не понимает что его сообщение вылилось в task. Mitigation: future option «Reply on @bot mention only»

## 12.11 Risks

### Технические
- TG long-poll timeout / disconnect → urllib retry
- LLM 429 / network error → SDK retry, затем skip — следующий tick подхватит
- Owner has no /start (TG) → `chat not found`, log info, skip recipient

### Продуктовые
- **Owner-routing accuracy <70%** → users перестанут нажимать кнопки. Mitigation: fallback на admin для low-confidence
- **Spam карточек** → user отключит уведомления. Mitigation: dedup + priority filter

### AI-риски
- **Prompt injection** через TG-сообщения → атакующий может попытаться сменить classifier результат. Mitigation: strict JSON schema validation, ignore "ignore previous instructions"-стиль
- **Confabulated owner names** → LLM придумывает имена не из team_members. Mitigation: post-LLM check «final_uid реально есть в team_members»

### Data risks
- **Soft-delete leak** через webhook (Q3) → удалённые задачи могли уйти. Mitigation: webhook фильтрует `deleted_at IS NULL`
- **PII в LLM prompts** (имена, контакты) → OpenAI processes data. Mitigation: documented в data-processing agreement

### Инфраструктурные
- **Single VM SPOF** → upgrade to managed group
- **No backups** → catastrophic data loss. TODO: pg_dump cron

### Security
- **TG bot token exposure** → если VM compromised, attacker может рассылать сообщения от имени бота. Mitigation: GCP Secret Manager, rate-limit на bot side

## 12.12 Open Questions

| ? | Why important | Who answers |
|---|---|---|
| Какие конкретно Slack-каналы слушать на MVP? | Зависит scope F-TT-11 | Артём |
| Какой email account для Email-ingest? | Нужен Gmail OAuth setup | Артём |
| Включать ли recurring tasks на MVP или Q2? | Scope creep | Product team |
| Нужен ли web admin UI до Q4 или можно весь lifecycle через TG? | Зависит от немочко-tech admin'ов | Артём |
| TIMEZONE: Europe/London или per-user из team_members? | UX для distributed team | Артём |
| Subscriptions нужны или только owner+admin recipients? | Scope of F-TT-20 | Артём |

---

# 13. Delivery Plan

## 13.1 Декомпозиция features

### F-TT-11 Slack ingest (Q2-2026, M) — **DELIVERED 2026-05-13 via FR-CR-05-162**

| Flow | Task | Subtask | AC | Status |
|---|---|---|---|---|
| Slack-msg → task | Setup Slack App scopes | Add bot scopes | history scopes enabled | ✅ |
|   |   | Enable Socket Mode + xapp token | Token saved in env (`/home/admin_/tg-listener.env`) | ✅ |
|   |   | Reinstall to workspace | New xoxb token issued (`xoxb-7293029581414-...`) | ✅ |
|   | Implement listener | `app/slack_ingest/listener.py` | Bolt App handles `message.channels` event | ✅ |
|   |   | Thread-context fetch | parent + last 5 replies via conversations.replies | ✅ (via `ContextRetriever`) |
|   |   | Channel-context fetch | last 10 messages within 5 min via conversations.history | ✅ (via `ContextRetriever`) |
|   |   | Anti-self-loop subtypes | skip bot_message + channel_join/leave + message_changed/deleted + thread_broadcast | ✅ (`_SKIPPED_SUBTYPES`) |
|   |   | InvocationType.passive (lowercase) | bug-fix 2026-05-13: uppercase variant doesn't exist on enum | ✅ + regression test |
|   |   | Real `Orchestrator` instance | bug-fix 2026-05-13: `orchestrator=None` blew up `persist_context_snapshot` | ✅ + regression test |
|   | Multi-task extraction (parity w/ TG) | Loop over `classification.tasks` instead of single `classification.task` | One message → N tasks, each its own row | ✅ + regression test |
|   |   | Intra-message dedup | Drop exact-title repeats inside one message before paying for cross-DB check | ✅ (`seen_titles` set) |
|   |   | Cross-DB dedup | LLM-based `check_duplicate()` vs open tasks; skip duplicates silently | ✅ (reused `app.services.task_dedup`) |
|   |   | Owner resolution chain | registry → LLM-picked uid → sender → admin (reused `_resolve_owner` from TG ingest) | ✅ + regression test |
|   | Wire to IngestService | Inlined classify + persist (avoid single-task `classify_and_persist`) | Test: msg "task X" → task in DB with source_kind=slack | ✅ |
|   | TG-card delivery | recipient = author + owner (mapped via slack_user_id↔telegram_user_id) + admins | Test: card delivered to mapped owner | ✅ (via `post_initial_card(for_slack_ingest=True)`) |
|   |   | Bypass cards.py source_kind=slack guard | bug-fix 2026-05-13: guard blocked DM delivery (legacy `slack_bot.cards` path) | ✅ + regression test |
|   | Disable Slack-side output | Бот не реагирует emoji, ack, DM | Slack channel остаётся silent | ✅ |
|   | Feature flag | `SLACK_INGEST_ENABLED=false` initially | Service no-op when off | ✅ |
|   | Tests | Unit + integration (`tests/test_slack_ingest.py`) | 11 tests, all AC pass | ✅ |
|   | Deploy to dev (1 channel) | docker run | Test in `#test_ceo_brain` (C0B33BQFUB1) | ✅ |
|   | Roll out to prod | Add bot to all channels | Production ready | 🔄 in progress |

#### F-TT-11 — Pipeline diagram (DELIVERED, FR-CR-05-162)

```
Slack message in #channel where bot is added
   │
   ├── Bolt @app.event("message") receives event
   │       Skip if: bot_user_id == self, bot_id set, subtype in _SKIPPED_SUBTYPES, empty text
   │
   ├── upsert_conversation + upsert_message (raw event stored)
   ├── EmployeeDirectory.observed() + ensure_channel_synced()  (best-effort)
   │
   ├── ContextRetriever.build → history_before + thread_messages
   ├── classifier.classify → IntentClassification (intent + tasks: list[TaskDraft])
   │       If intent ≠ create_task or tasks == [] → log "slack_ingest_no_tasks" and exit
   │
   ├── For each TaskDraft td:
   │       _resolve_owner(td, known_employees, sender_uid, sender_name, admin_uid)
   │
   ├── persist_context_snapshot (once)
   │
   └── For each TaskDraft td (loop 2):
           intra-message dedup → seen_titles
                  skip if td.title.lower() already seen → log "slack_ingest_skipped_intra_message_duplicate"
           cross-DB dedup → check_duplicate(td, llm_backend)
                  skip if dup.is_duplicate → log "slack_ingest_skipped_duplicate"
           persist_inference + create_draft + create_task_from_draft (source_kind=slack)
           log "slack_ingest_task_created"
           post_initial_card(for_slack_ingest=True)  → DM to author + owner + admins via TG
                  emits "slack_ingest_tg_sender_disabled" or "slack_ingest_tg_card_post_failed" on failure
```

### F-TT-14 Morning digest (Q2-2026, S)

| Flow | Task | AC |
|---|---|---|
| Owner получает дайджест 08:00 | DB query for active tasks | `WHERE owner_user_id = ? AND status NOT IN ('done','cancelled') AND (due_date <= today OR priority='urgent')` |
|   | Format message | Заголовок + список с emoji + clickable links |
|   | Cron-trigger (8:00 local TZ) | systemd timer или внутренний loop |
|   | Send via TG bot | `sendMessage` per uid |
|   | Skip if no active tasks | Empty digest → no message |

### F-TT-13 Recurring tasks (Q2-2026, M)

| Flow | Task | AC |
|---|---|---|
| Recurring rule fires | DB migration: `recurring_task_rules` | Table created |
|   | Cron-loop в listener (5 мин) | Scan enabled AND next_run_at <= NOW() |
|   | Spawn Task | INSERT новая task с template_* |
|   | Update next_run_at | По schedule_kind |
|   | Ship reminder | TG card к template_owner |

### F-TT-19 Google Tasks 2-way sync (Q3-2026, L)

| Flow | Task | AC |
|---|---|---|
| Push: local change → GTasks | Hook в Task.save() | POST/PATCH в gtasks.tasks.insert/patch |
|   | Mapping fields | title, notes (desc), due (RFC3339), status |
|   | Store gtask_id | в tasks.google_task_id |
| Pull: GTasks change → local | Periodic poll (60s) | tasks.list на default tasklist |
|   | Conflict resolution | last-write-wins by updated_at |
|   | New tasks from GTasks | INSERT с source_kind='google_tasks', owner=admin |

## 13.2 Эпики

| Epic | Состав | Effort | Quarter |
|---|---|---|---|
| E-TT-1: Stabilise MVP | F-TT-01..10 + bugfixes | 1 неделя | Q2-2026 |
| E-TT-2: Slack ingest | F-TT-11 | 2 недели | Q2-2026 |
| E-TT-3: Email ingest | F-TT-12 | 2 недели | Q2-2026 |
| E-TT-4: Recurring + digests + reminders | F-TT-13..17 | 3 недели | Q2-2026 |
| E-TT-5: Manual /task + audit | F-TT-18 | 1 неделя | Q2-2026 |
| E-TT-6: Google Tasks 2-way sync | F-TT-19 | 3 недели | Q3-2026 |
| E-TT-7: Subscriptions + voice commands | F-TT-20, 23 | 2 недели | Q3-2026 |
| E-TT-8: Web admin UI | F-TT-21, 22 | 6 недель | Q4-2026 |
| E-TT-9: Webhook export + Notion exporter | F-TT-24, 25 | 2 недели | Q4-2026 |

---

# 14. Assumptions, Out of Scope, Traceability

## 14.0a Assumptions (Допущения)

| ID | Assumption | Где используется | Риск если неверно | Как проверить |
|---|---|---|---|---|
| A-TT-1 | Supabase view возвращает свежие сообщения <30s после post | NSM target | Если 1+ мин — NSM не достижим | Замер на 100 сообщений |
| A-TT-2 | LLM intent_classifier accuracy ≥90% (task vs chitchat) | M-Q5 | Иначе spam карточек | Manual sample 100 messages |
| A-TT-3 | team_members справочник содержит ≤100 человек | LLM owner_node token budget | При 1000+ выйдем из window | Audit table size |
| A-TT-4 | Owner team_members имеет telegram_user_id | TG card delivery | Иначе fallback на admin | DB query coverage |
| A-TT-5 | Owners подписаны на бот (`/start`) | Card delivery rate | Иначе `chat not found` 100% | Onboarding checklist |
| A-TT-6 | TG long-poll выдерживает поток до 100 msg/min | Throughput | При >100 — Bot API throttle | Stress test |
| A-TT-7 | Дедлайны редко даются с точностью до часа («к пятнице» а не «в 14:30») | date_node simplification | Если 50%+ нужно time → переписать prompt | Sample analysis |
| A-TT-8 | Recurring задачи будут ≤20 active rules | Cron-loop scaling | При 100+ нужен индекс на next_run_at | Audit при v1 |
| A-TT-9 | Google Tasks 2-way sync (Q3) — quota 50k requests/day достаточна | GTasks throughput | При >50k → throttle | Estimate from current task volume |
| A-TT-10 | TIMEZONE един для всех owner'ов (Europe/London) | Digest timing | Distributed team будет получать в неудобное время | Survey owners |

## 14.0b Out of Scope (Что не входит в продукт)

**Task Tracker НЕ делает:**
- ❌ Транскрибация / summary встреч — это Note Taker (см. SPEC_NOTE_TAKER_v0.1.md)
- ❌ Project management features (Gantt, dependencies, sprints, milestones) — слишком far из CEO scope
- ❌ Time tracking / billable hours — out of vision
- ❌ Performance reviews / 1-on-1 notes — separate tool
- ❌ Internal messaging между users (это просто слой над уже-существующими каналами TG/Slack)
- ❌ Web admin UI до Q4 — все управление через TG bot и DB direct
- ❌ Native iOS/Android apps — только TG (mobile-friendly)
- ❌ Voice commands в TG (Q3 roadmap)
- ❌ Calendar integration для задач (нет «заблокировать слот в календаре под задачу») — Q4
- ❌ File attachments к задачам — out of MVP
- ❌ Comments на задачах (внутренний chat) — out of MVP
- ❌ Multi-tenant (один tenant = один CEO+team) — будет если нужно

## 14.0c Traceability Matrix

### Task Tracker Traceability

| Feature | User Story | User Flow | Use Case | BDD Scenario | FR | NFR | Component | Test |
|---|---|---|---|---|---|---|---|---|
| F-TT-01 (TG ingest live) | US-TT-1 | 8.1: TG → Supabase → Listener | UC-TT-01 | "Простая задача с явным owner" | FR-TT-1.1, 1.2 | NFR-TT-P.1, R.1 | TelegramIngestService | T-TT-001 |
| F-TT-02 (Intent classifier) | US-TT-1 | 8.1: classifier | UC-TT-01 | "Сообщение классифицировано как chitchat" | FR-TT-2.1, 2.2 | NFR-TT-O.1 | IntentClassifier | T-TT-002 |
| F-TT-03 (Drafts loop) | US-TT-1 | 8.1: drafts | UC-TT-01 | "Drafts loop создаёт ActionDraft" | FR-TT-3.1..3.5 | NFR-TT-C.1 | DraftsGraph | T-TT-004..007 |
| F-TT-04 (Owner LLM-router) | US-TT-1 | 8.1: owner_node | UC-TT-01 | "Owner не найден — fallback admin" | FR-TT-3.3 | NFR-TT-O.1 | OwnerNode + team_members | T-TT-005 |
| F-TT-05 (Deadline parser) | US-TT-1 | 8.1: date_node | UC-TT-01 | (implicit) | FR-TT-3.4 | NFR-TT-O.1 | DateNode | T-TT-006 |
| F-TT-06 (Priority inference) | US-TT-1 | 8.1: priority_node | UC-TT-01 | (implicit) | FR-TT-3.5 | NFR-TT-O.1 | PriorityNode | T-TT-007 |
| F-TT-07 (Dedup) | US-TT-1 | 8.1: dedup_node | UC-TT-01 | "Дубликат" | FR-TT-4.1, 4.2, 4.3 | NFR-TT-D.1 | DedupNode | T-TT-008 |
| F-TT-08 (Card lifecycle) | US-TT-2 | 8.2: button presses | UC-TT-02 | "Принять", "Делегировать", "Закрыть" | FR-TT-7.1..7.4 | NFR-TT-U.1, U.2 | CallbackHandler + TaskCardBuilder | T-TT-014, 015, 016 |
| F-TT-09 (NT tasks pickup) | US-TT-4 | 8.x: NT INSERT → TT picks up | UC-TT-05 | (no full Gherkin yet, depends NT) | FR-TT-1.6 | NFR-TT-P.1 | (через DB shared) | T-TT-040 |
| F-TT-10 (Multi-recipient delivery) | US-TT-1, 2 | 8.1: TG cards out | UC-TT-01, 02 | "TG card отправляется Алине + admin" | FR-TT-6.1, 6.2, 6.3, 6.4 | NFR-TT-U.1 | TaskCardBuilder.post_initial_card | T-TT-011, 013 |
| F-TT-11 (Slack ingest) TODO | US-TT-5 | 8.x (TODO): Slack → ingest | UC-TT-03 | (TODO) | FR-TT-1.3, 1.4 | NFR-TT-S.2 | SlackIngestService (TODO) | T-TT-020, 021 |
| F-TT-12 (Email ingest) TODO | (no full US) | (TODO) | UC-TT-04 | (TODO) | FR-TT-1.5 | NFR-TT-* | EmailIngestService (TODO) | T-TT-030 |
| F-TT-13 (Recurring tasks) TODO | US-TT-6 | (TODO) | UC-TT-09 | (TODO) | FR-TT-10.1..10.3 | NFR-TT-* | RecurringScheduler (TODO) | T-TT-060 |
| F-TT-14 (Morning digest) TODO | US-TT-3 | (TODO) | UC-TT-06 | (TODO) | FR-TT-8.1, 8.3, 8.4 | NFR-TT-U.* | DigestService (TODO) | T-TT-070 |
| F-TT-15 (Evening digest) TODO | (mirror US-TT-3) | (TODO) | UC-TT-07 | (TODO) | FR-TT-8.2 | NFR-TT-U.* | DigestService (TODO) | T-TT-071 |
| F-TT-16 (Deadline reminders) TODO | (no full US) | (TODO) | UC-TT-08 | (TODO) | FR-TT-9.1..9.3 | NFR-TT-* | DeadlineReminderService (TODO) | T-TT-080 |
| F-TT-17 (Status notifications) TODO | (no full US) | (TODO) | (no UC yet) | (TODO) | (no FR yet) | NFR-TT-* | StatusNotificationService (TODO) | T-TT-090 |
| F-TT-18 (Manual /task) TODO | (no full US) | (TODO) | UC-TT-11 | (TODO) | FR-TT-1.7 | NFR-TT-* | TG bot command handler (TODO) | T-TT-050 |
| F-TT-19 (GTasks 2-way sync) TODO | US-TT-7 | (TODO) | UC-TT-10 | (TODO) | FR-TT-11.1..11.4 | NFR-TT-I.1 | GoogleTasksSync (TODO) | T-TT-100 |
| F-TT-20 (Subscriptions) TODO | (no full US) | (TODO) | UC-TT-12 | (TODO) | FR-TT-12.1, 12.2 | NFR-TT-* | task_subscriptions table + handler (TODO) | T-TT-120 |

### Architecture Traceability

| Requirement ID | Use Case | User Flow | Client Component | Service | AI Service | Entity | Test |
|---|---|---|---|---|---|---|---|
| FR-TT-1.1 | UC-TT-01 | 8.1 step "Listener poll Supabase" | (none) | TelegramIngestService.prepare_drafts | (none direct) | processed_telegram_messages | T-TT-001 |
| FR-TT-2.1 | UC-TT-01 | 8.1 step "intent classifier" | (none) | (none direct) | IntentClassifier | (transient) | T-TT-002 |
| FR-TT-3.3 | UC-TT-01 | 8.1 step "owner_node" | (none) | (none direct) | OwnerNode | team_members | T-TT-005 |
| FR-TT-3.4 | UC-TT-01 | 8.1 step "date_node" | (none) | (none direct) | DateNode | tasks.due_date | T-TT-006 |
| FR-TT-4.1 | UC-TT-01 | 8.1 step "dedup_node" | (none) | (none direct) | DedupNode | tasks (existing) | T-TT-008 |
| FR-TT-5.1 | UC-TT-01 | 8.1 step "INSERT tasks" | (none) | TelegramIngestService | (none) | tasks | T-TT-009 |
| FR-TT-6.1 | UC-TT-02 | 8.2 step "card delivery" | TG card | TaskCardBuilder.post_initial_card | (none) | tasks (card_channel/card_ts) | T-TT-011 |
| FR-TT-7.1 | UC-TT-02 | 8.2 step "button press" | TG inline keyboard | CallbackHandler | (none) | tasks.status | T-TT-014 |
| FR-TT-7.3 | UC-TT-02 | 8.2 step "audit row" | (none) | CallbackHandler | (none) | task_status_changes | T-TT-015 |
| FR-TT-1.6 | UC-TT-05 | 8.x NT pickup | (none, indirect via TG card) | (через shared DB) | (none) | tasks (source_kind=zoom/fireflies) | T-TT-040 |
| FR-TT-8.1 | UC-TT-06 | (TODO) Q2 | TG morning digest message | DigestService | (none) | tasks query | T-TT-070 (TODO) |
| FR-TT-10.1 | UC-TT-09 | (TODO) Q2 | (none) | RecurringScheduler | (none) | recurring_task_rules | T-TT-060 (TODO) |
| FR-TT-11.1 | UC-TT-10 | (TODO) Q3 | (none) | GoogleTasksSync.push | (none) | tasks.google_task_id | T-TT-100 (TODO) |

# 15. Appendices

## 15.1 Glossary

- **Task Tracker** — agent для task lifecycle (create → assign → distribute → close)
- **Drafts loop** — серия LLM-вызовов (title/desc/owner/date/priority/dedup)
- **TG card** — Telegram DM с inline-keyboard представляющая task
- **Recipient** — пользователь, получающий копию TG-карточки (author + owner + admins)
- **Recurring rule** — шаблон periodically генерирующий задачи (daily/weekly/monthly/cron)
- **Subscription** — пользователь подписан на чужую задачу для notifications

## 15.2 References

- `docs/specs/task_tracker/` — detailed sub-specs
- `SPEC_NOTE_TAKER_v0.1.md` — Note Taker spec (sister agent)
- `app/telegram_bot/listener.py` — main TG-listener code
- `app/telegram_ingest/service.py` — TelegramIngestService
- `app/telegram_bot/cards.py` — card builder + recipient resolution
- `app/intent/classifier.py` — intent classification
- `app/intent/llm_backends.py` — OpenAIBackend
- `app/services/team_members.py` — team справочник helpers

---

**Версия:** v0.1, 2026-05-08
**Maintainer:** Артём Соколов
**Lifecycle:** spec обновляется вместе с PR'ами кода. Каждый новый feature → US + UC + FR + tests + delivery plan в одном PR с code.
