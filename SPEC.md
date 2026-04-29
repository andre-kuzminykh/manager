# Slack Task Manager — Specification

Version: 5 (Base + CR-01 + CR-02 + CR-03 refined + CR-04)

This document consolidates:

- **Base SPEC** — initial product spec (passive detection, explicit mention,
  message shortcuts, DB persistence, Google Sheets + Google Tasks sync).
- **CR-01** — richer task lifecycle (Backlog → To Do → In Progress → Review →
  Done), explicit owner picker from an allowed list, workload-aware deadline
  proposal, subscribe/unsubscribe, start-work button, subscriber broadcasts,
  and daily / weekly / deadline digests.
- **CR-02** — conversational UX: @mention always produces a draft widget,
  bot asks missing fields in the thread, user replies update the card in
  place via `chat.update`, widget morphs on Confirm and is deleted on Ignore,
  daily digest gains a Tracking section with a Manage-subscriptions modal,
  one-task / one-DM-thread anchoring so all notifications stack under the
  same conversation.
- **CR-03** — admin-run team operations: full ingestion of every message in
  bot-visible channels, automatic employees directory from Slack metadata,
  editable tasks with audit trail, weekly plan on Sundays, artifact on
  completion, admin digests and daily reminder threads in the source
  message. **Reverted from original CR-03 proposal:** the always-create +
  admin-only confirmation flow for passive detection has been replaced by
  CR-04's offer-first draft card (see §10 below).
- **CR-04** — structured extraction pipeline wired as a LangGraph
  state machine (`detect → [describe | owner | date] → assemble`),
  offer-first passive UX with Accept/Edit/Reject, mention-with-
  follow-up parity for assumed owners, date-phrase stripping in
  title/description, Whisper transcription for Slack voice notes, and
  an LLM-first date node on a stronger model (gpt-4o) backed by a
  Python validator/fallback.

## 1. Operating modes

### 1.1 Tag mode (explicit @mention)

Triggered when a user mentions the bot in a conversation or invokes a message
shortcut. The bot:

1. Reads the source message + recent context.
2. Extracts the task, then asks clarifying questions if fields are missing:
   - owner — picked from an **allowed owners list** (from config);
   - deadline — proposed automatically (see §3.3) and editable;
   - confirmation required?
   - decomposition needed?
3. Opens a modal prefilled with the extracted fields.
4. On **Confirm**, persists the task and announces it in the thread.

### 1.2 Auto mode (passive detection)

Triggered when the bot is a participant in an MPIM, DM, or channel. The bot:

1. Listens to `message.*` events.
2. Classifies intent with confidence thresholds.
3. Posts a **draft card** with:
   - short title + description;
   - source badge (channel + user);
   - **Open source** link to the original Slack message;
   - expandable **context** view (~10 last messages that fed the extraction);
   - proposed owner (from allowed list);
   - proposed deadline;
   - explanation of why the system thinks this is a task.
4. User confirms or rejects. Nothing is persisted until the user confirms.

## 2. Functional requirements

Original FR-1..FR-12 from the base SPEC apply unchanged with one
exception: meeting capture (FR-3 / FR-7 / FR-9 / FR-10 meeting
branches) was retired in **FR-CR-04-19** — see §10.2. CR-01 adds:

### 2.1 Allowed owners (FR-CR-1)

A configurable list of users who can be assigned as task owners.

- Loaded from `ALLOWED_OWNERS` env (JSON list of
  `{slack_user_id, display_name}`) and/or a DB-backed registry.
- The draft card and modal expose a single-select picker constrained to this
  list.
- Orchestrator may pre-fill the owner based on natural-language hints in the
  source text but must validate it against the allowed list.

### 2.2 Workload-aware deadline (FR-CR-2)

`WorkloadEstimator.propose_due_date(owner_user_id, estimated_minutes)`:

- Pulls the owner's open tasks (`backlog`, `todo`, `in_progress`, `review`).
- Sums their `estimated_minutes`.
- Chooses the earliest business date on which the new task can finish within a
  daily budget (default 6 hours/day).
- Output is a **proposal**; the user can override it in the modal.

### 2.3 Lifecycle (FR-CR-3)

```
Backlog  ──▶  To Do  ──▶  In Progress  ──▶  Review  ──▶  Done
  ▲             │                 ▲              │
  └─ (reopen) ──┘                 └─────────────┘
```

- **Backlog** — captured, not scheduled into the current week.
- **To Do** — scheduled and waiting to start.
- **In Progress** — assignee clicked **Начать работу** (Start work).
- **Review** — submitted for review / approval.
- **Done** — completed.

A Task also carries `is_current_week: bool`. On creation, defaults to
`True` when the proposed due date is ≤ 7 days away; otherwise `False` and
the initial status is `backlog`.

### 2.4 Start-work action (FR-CR-4)

- Task cards expose a **Начать работу** button, enabled only for the
  task's owner.
- Click → transition to `in_progress`; record `started_at`; append a
  `TaskStatusHistory` row; broadcast to every subscriber.

### 2.5 Subscriptions (FR-CR-5)

- Every task has a list of subscribers.
- On task creation, the owner and the source-message author are
  auto-subscribed.
- Task cards include a **Подписаться / Unsubscribe** toggle.
- Subscribers receive DM notifications on:
  - status change (including start-work);
  - approaching deadline;
  - overdue;
  - completion.

### 2.6 Digests and reminders (FR-CR-6)

- **Daily digest** (morning): per subscriber — today's tasks, approaching
  deadlines, overdue tasks.
- **Weekly digest**: week's planned tasks, statuses, attention items.
- **Deadline reminder**: pre-deadline nudge on each in-progress task.

Scheduling is performed outside the app by cron / Cloud Scheduler invoking
`python -m ops.send_digest --type {daily|weekly|deadlines}`.

### 2.7 Status history (FR-CR-7)

Every status transition appends a row to `task_status_history` with:

- `task_id`, `from_status`, `to_status`, `changed_by_slack_user_id`,
  `reason`, `at`.

## 3. Data model changes (CR-01)

### 3.1 TaskStatus (replaces old values)

Old: `open / in_progress / done / cancelled`
New: `backlog / todo / in_progress / review / done`

### 3.2 Task — additional columns

- `started_at: datetime | None`
- `completed_at: datetime | None`
- `estimated_minutes: int | None` (workload heuristic)
- `is_current_week: bool` default `True`

### 3.3 New tables

- `task_status_history (id, task_id, from_status, to_status,
  changed_by_slack_user_id, reason, at)`
- `task_subscriptions (id, task_id, slack_user_id, created_at)`
  - `UNIQUE(task_id, slack_user_id)`

## 4. UX — Slack card buttons

Draft card (pre-confirm): **Confirm · Edit · Ignore**

Task card (post-confirm): **Начать работу · Submit for review · Mark done ·
Подписаться / Отписаться · Show context**

(CR-02: the **Open source** URL button was removed — the card is posted in
the source thread, so the back-link is redundant. The permalink is still
carried in the post-confirm DM message.)

## 4.1 CR-02 — Conversational mention UX

### FR-CR-02-1: Mention-always-replies (fallback)

An explicit @mention is always a signal to capture something. If the LLM
returns `no_action` or otherwise fails to produce a draftable payload, the
bot **must** synthesise a minimal `create_task` draft with the cleaned
source text as the title (capped at 200 characters) and post the standard
draft card. Only a bare mention with no text (`<@bot>` alone) falls back to
an informational reply asking the user to add text.

### FR-CR-02-2: Follow-up questions in thread

After posting the draft card the bot asks in the same thread for the first
missing field (ordered: title → due_date → owner). The first question is
prefixed with `:memo: Captured: *<title>*.` so the user sees what was
recorded.

### FR-CR-02-3: Reply-in-thread updates the card

Any thread reply to the mention message (while
`action_drafts.awaiting_field` is set) is parsed according to the awaited
field:

- `due_date` / `datetime_at` — ISO short-circuit first, then `dateparser`
  (`ru` + `en`) with Russian preposition + genitive day-name normalisation
  so *«до пятницы»* resolves to the next Friday;
- `owner` — resolved against `ALLOWED_OWNERS` via `resolve_owner_hint`;
- `title`, `description`, `notes` — stored as free text;
- `participants` — split on commas.

On success the card is updated in place via `chat.update`, and the bot
either asks the next missing field or posts a
`:white_check_mark: Все поля собрал. Жми Confirm` nudge when everything is
known.

If the reply cannot be parsed (`до чего-то`, `не знаю`), the bot re-asks
the same field with a hint.

### FR-CR-02-4: Draft card disappears on Confirm / Ignore

After a successful **Confirm** the draft widget is removed via
`chat.delete` and replaced with the short `✅ Task #N created …` summary
plus the full task card. On finalize failure the widget stays so the user
can retry via **Edit** / **Confirm**. **Ignore** also deletes the widget
and marks the draft as `ignored` in DB.

### FR-CR-02-5: No Open-source button on task cards

The URL button `Open source` has been removed. The card is posted in the
source thread, so the back-link is implicit. The source `permalink` is
still rendered inside the post-confirm DM.

### FR-CR-02-6: Daily digest Tracking section

The daily digest is now sent to every user who either owns or subscribes to
at least one open task (previously owners only). Layout:

1. *Your tasks for YYYY-MM-DD* — Today / Approaching / Overdue — owned
   tasks only.
2. Divider.
3. *Отслеживаемые (N)* — tasks the user subscribes to but does not own,
   each showing status, due, and owner.
4. Actions block with a **Управлять подписками** button.

### FR-CR-02-7: Manage-subscriptions modal

The button opens a modal listing every task the user subscribes to with a
per-row **Отписаться** button. Clicking unsubscribe removes the row and
refreshes the modal in place via `views.update`.

### NFR-CR-02-1: Idempotent upserts for duplicate Slack events

`upsert_conversation` and `upsert_message` use PostgreSQL
`INSERT ... ON CONFLICT DO NOTHING` (and a savepoint+IntegrityError
fallback for SQLite tests). Slack retries or double-dispatched events
(`app_mention` + `message.channels` for the same text) never raise on the
shared rows.

### NFR-CR-02-2: Mention handler never goes silent

Every code path in `handle_app_mention` either posts a draft widget, a
synthesised widget, a `:thinking_face: add text` hint, or — on uncaught
exceptions — a `:warning:` fallback. `ack()` is always called first.

## 4.2 CR-02 — Data model delta

Migration `0003_draft_followup`:

- `action_drafts.card_channel: str | None`
- `action_drafts.card_ts: str | None`
- `action_drafts.awaiting_field: str | None`
- Index `ix_action_drafts_thread` on `action_drafts.slack_message_ts` for
  fast thread-reply lookup.

## 5. Acceptance criteria

Inherits the base SPEC criteria and adds:

- Bot exposes the **Начать работу** button on tasks owned by the clicking
  user; clicking transitions the task to `in_progress`.
- Subscribers receive a DM for every state change of tasks they follow.
- Task status enum supports exactly `backlog / todo / in_progress / review /
  done`.
- Owner picker is limited to the allowed list and hinted from NL.
- Proposed deadline accounts for the owner's existing workload.
- Daily, weekly, and deadline digests are delivered via the `send_digest`
  CLI.
- (CR-02) Every @mention produces a draft widget. If the LLM cannot extract
  structure, the widget's title is the source text and a follow-up question
  appears below.
- (CR-02) A user reply in the thread updates the same card via
  `chat.update`; no extra card is posted.
- (CR-02) Confirm / Ignore delete the draft widget.
- (CR-02) Daily digest includes a Tracking section and a working
  **Управлять подписками** modal with per-task unsubscribe.

## 6. Non-functional requirements

All NFR-1..NFR-11 from the base SPEC carry over. In addition:

- **NFR-CR-1**: Broadcasts to subscribers go through the rate-aware sender
  (1 msg/s per channel with 429 Retry-After handling).
- **NFR-CR-2**: Digests are idempotent per (user, day) — rerunning the cron
  does not duplicate messages.
- **NFR-CR-3**: Status transitions are atomic with their history row.

## 7. Migration path

Alembic revision `0002_cr_amendments`:

- ENUM `task_status` rebuilt with new values.
- Adds columns to `tasks`.
- Creates `task_status_history`, `task_subscriptions`.

## 8. Open items (explicitly deferred)

- Per-user notification schedule / quiet hours.
- External tracker sync (Jira / Linear) — tasks live in our DB as source of
  truth.

## 9. CR-03 — Admin-run team operations

### 9.1 Product goals

- **Admin as source-of-truth**. One (or more) designated admin(s) see every
  detected task, approve / edit / reject in bulk, and watch progress.
- **Automatic employees directory**. Drop the static `ALLOWED_OWNERS` JSON
  blob — the bot learns every team member from Slack metadata and keeps it
  fresh.
- **Always capture**. Any detected task is persisted the moment confidence
  is high enough, even with missing fields. Admin can edit or reject; the
  audit log keeps a full trail.
- **Assignee loop**. Assignees get pinged in the source thread daily until
  a task reaches `done`. Completion requires an artifact attachment.
- **Planning rhythm**. Sunday evening: weekly plan for every assignee.
  Weekday evening: admin's next-day approval digest. Weekday morning:
  per-assignee reminders + admin's live watch-list.

### 9.2 Functional requirements

#### FR-CR-03-1 — Message ingestion & Employees directory

All messages from channels / DMs the bot is a member of are persisted to
`slack_messages` regardless of whether they produced a task. For every
unique author we upsert an `Employee` row populated via Slack `users.info`:

- `slack_user_id` (PK), `team_id`
- `display_name`, `real_name`, `email`, `title`
- `timezone`, `is_bot`, `is_admin`
- `last_seen_at`, `profile_raw` (full payload, JSON)

Updates are at-most once per `EMPLOYEE_REFRESH_TTL_SECONDS` (default 24 h)
to respect Slack rate limits.

#### FR-CR-03-2 — Admin registry

Admins are configured via `ADMIN_SLACK_USER_IDS` (comma-separated list) and
reflected by setting `Employee.is_admin = True`. Changing the env → DB flag
flips within a minute. A helper `is_admin(user_id) -> bool` is used across
all admin-gated code paths.

#### FR-CR-03-3 — Passive detection policy (REVERTED, see CR-04)

> **Historical:** the original proposal was to auto-create a `Task` whenever
> passive classification reached `INTENT_CONFIDENCE_HIGH` and hand it to
> admins for review. Product feedback after rollout found the admin-only
> flow too gated for a small team. CR-04 replaces this with a user-facing
> pre-filled draft card (Accept / Edit / Reject). The requirement ID is
> retained for traceability but the behaviour described here is no longer
> implemented; see **FR-CR-04-6**.

#### FR-CR-03-4 — Admin confirmation (REVERTED, see CR-04)

> **Historical:** admin DM + ephemeral "Confirm / Edit / Reject" on every
> auto-created task. Replaced by the user-facing draft card in CR-04.
> Admins keep power over tasks via **FR-CR-03-5** (Edit button is available
> to admin AND assignee) and the admin digests
> (**FR-CR-03-8**).

#### FR-CR-03-5 — Editable tasks with history

`Edit` button on the task-card is available to admin AND to the assignee.
Opens the modal with current values. Submit updates fields and emits an
audit row `action=task_edited` with a diff of changed fields. Thread
reminders refresh to the new deadline/owner.

#### FR-CR-03-6 — Assignee weekly plan (Sundays)

Sunday 20:00 local tz: per-assignee DM listing every `backlog` task with
`due_date` in the upcoming Mon–Sun week. Each row has **Принять** /
**Позже** buttons.

- **Принять** → task moves to `todo`.
- **Позже** → task stays in `backlog`; admin gets a ping if more than 2
  tasks deferred.

#### FR-CR-03-7 — Start / Review / Complete flow

- **Начать** → status `todo` / `backlog` → `in_progress` and records
  `started_at` (existing).
- **Завершить** → opens a modal asking for an **artifact** (URL or text,
  at least one required). On submit → status `done`, `completed_at`,
  `completion_artifact`. Thread reminder is stopped, admin DM is
  updated with the artifact.
- **Вернуть в бэклог** → `in_progress` → `backlog`, history row; thread
  reminder re-engaged.

#### FR-CR-03-8 — Admin watch-list digest

Morning (9:00 admin tz) DM to each admin:

- tasks they own (same as regular digest),
- plus *Admin watch*: every `in_progress` / `review` task across the team,
  with assignee tag, due, latest status.

Evening (20:00 admin tz) DM to each admin:

- *Tomorrow*: tasks due tomorrow by assignee,
- *Stale*: `in_progress` tasks with no status reply in 2 + days.

#### FR-CR-03-9 — Daily reminders in source thread

Every weekday 10:00 local tz the bot posts a message in the source thread
for each open task, tagging the assignee:

- `in_progress`: `:raised_hand: <@assignee> задача #N — как прогресс?`
- `todo` (due this week): `:calendar: <@assignee> на этой неделе ожидаем:
  *<title>* — до <due>`
- `review`: `:eyes: <@reviewer> нужен ревью задачи #N`

Replies in the thread are ingested as `status_pings` audit rows. Bot
stops nudging once status hits `done`.

#### FR-CR-03-10 — Live admin updates

Every edit / status change / artifact attachment:

- `chat.update` the admin's DM card (already wired for `dm_channel/ts`).
- Thread-broadcast to subscribers under the anchor DM (already wired).
- Append audit row.

### 9.3 Non-functional requirements

- **NFR-CR-03-1** — Slack `users.info` respects rate limits: at-most-once per
  `EMPLOYEE_REFRESH_TTL_SECONDS`; on 429 backoff via the rate-aware sender.
- **NFR-CR-03-2** — Every admin action writes an audit row
  (`admin_review` / `task_edited` / `admin_rejected`).
- **NFR-CR-03-3** — Weekly / daily / evening digests are idempotent per
  (user, date / week) via the existing `audit_logs`-backed mechanism.
- **NFR-CR-03-4** — Thread reminders deduplicate: we never post two pings
  for the same task-thread-day (`reminder:<task_id>:<YYYY-MM-DD>` audit).
- **NFR-CR-03-5** — Ephemeral admin confirmations degrade to plain thread
  reply when `chat.postEphemeral` isn't available in the context (DMs).

### 9.4 Data model delta

Migration `0006_employees_and_admin`:

- `employees` (slack_user_id PK, team_id, display_name, real_name, email,
  title, timezone, is_bot, is_admin, last_seen_at, profile_raw, timestamps)
- `tasks.completion_artifact: Text | None`
- `tasks.completion_artifact_kind: Enum("url","text","file") | None`
- Index `ix_slack_messages_user_id` for per-employee lookups.

Migration `0007_admin_review_audit` (later): no new tables — we reuse
`audit_logs` with namespaced actions.

### 9.5 Acceptance criteria (CR-03 additions)

- Every channel the bot is in backs an up-to-date `employees` row per author.
- An admin receives a confirmation DM + ephemeral thread reply for each
  auto-created task.
- Rejecting an admin-review task deletes it and logs the reason.
- On Sunday at 20:00 every assignee receives a weekly-plan DM.
- On weekday 10:00 the bot pings each open task's thread; pinging ceases
  once the task is `done`.
- Marking a task `done` requires a non-empty artifact.
- Admin's DM card always reflects the current status / artifact within one
  `chat.update` cycle of any edit.

## 10. CR-04 — Structured pipeline, offer-first passive, audio input

### 10.1 Product goals

- **Reliable extraction on small models**. Split the monolithic intent
  prompt into focused stages so gpt-4o-mini doesn't have to juggle
  detection + title + owner + date in one breath. Each concern has its
  own prompt, its own tool schema, its own graph node.
- **Everything is an LLM prompt**. Detection, description, owner,
  date — all four concerns are answered by focused LLM calls. The
  Python date resolver stays as a validator and fallback: if the LLM
  emits null or a back-dated / malformed ISO, the resolver rescues.
- **Offer-first passive**. A passive message never auto-creates a task.
  The bot posts a pre-filled draft card with Accept / Edit / Reject and
  — in parallel — asks the missing field in the source thread. The
  author decides whether the task lands in the tracker.
- **Mention parity for assumed owners**. When @mention creates a task
  and the owner is only a fallback to the message author, the bot asks
  "кому назначаем?" in the thread, same machinery as passive.
- **Voice notes are first-class input**. Slack audio attachments are
  transcribed via Whisper and fed into the pipeline alongside any
  typed caption.

### 10.2 Functional requirements

#### FR-CR-04-1 — Detection node

A focused LLM call (`app/intent/detect_prompt.py`) answers a single
question: *is this message a task?* Output schema:
`{is_task: bool, confidence: number in [0,1], reasoning: string}`.
When `is_task` is false, the LangGraph router skips every extraction
node and the pipeline returns `no_action` directly from `assemble`.
Model: the default backend model (gpt-4o-mini).

#### FR-CR-04-2 — Parallel extraction via LangGraph

The pipeline is compiled as a `StateGraph`:

```
START → detect → is_task?
                  │
                  ├── false ──▶ assemble ─▶ END
                  │
                  └── true  ──▶ describe ┐
                                owner    ├─▶ assemble ─▶ END
                                date     ┘
```

LangGraph's conditional-edges fan-out runs `describe`, `owner`, and
`date` concurrently. Each extractor is pure with respect to the
state and writes only its own keys, so parallel merges are safe.

- **describe**: title / description / priority (LLM,
  `title_prompt.py`). Both `title` and `description` are piped
  through `strip_date_phrase` so date hints end up only in
  `due_date`.
- **owner**: assignee (LLM, `owner_prompt.py`). Sees the same
  10-message context window to distinguish "питчдек для Ивана"
  (audience) from "Иван, сделай питчдек" (assignee). A "no assignee"
  answer overrides whatever detect/describe may have guessed.
- **date**: see FR-CR-04-3 below.

#### FR-CR-04-3 — Date node (LLM-first, Python-validated)

The `date` node is a dedicated LLM call with its own focused prompt
(`app/intent/date_prompt.py`). It runs on a **stronger model**
(`OPENAI_DATE_MODEL`, default `gpt-4o`) because gpt-4o-mini was
demonstrably unreliable on relative-date phrases. Output:
`{due_date: ISO or null, reasoning: string}`.

A Python resolver (`app/intent/date_resolver.py`) runs as a **safety
net**:

1. The node parses the LLM's `due_date`. Only accepted when it parses
   to a valid ISO date AND the date is strictly after `current_date`
   (or equal to it if the source text literally contains
   "сегодня"/"today"). Back-dated hallucinations are rejected.
2. If the LLM's answer was null OR rejected, the node calls
   `resolve_due_date(source_text, today)` and uses its result.
3. If both return null, `due_date` stays null and the follow-up loop
   asks the user.

The resolver is also used by `strip_date_phrase` (FR-CR-04-5) to
clean titles and descriptions, so the resolver's set of recognised
patterns still defines the "canonical" Russian / English phrasings
the product handles without LLM:

**Absolute formats:**
- ISO `YYYY-MM-DD` — `2026-05-01`.
- Russian / European numeric: `DD.MM.YYYY`, `DD.MM.YY`, `DD.MM`,
  `DD/MM/YYYY`, `DD/MM`. Bare DD.MM (no year) picks the next future
  occurrence. With optional leading preposition `до / к / на / by`.

**Relative phrases:**
- `сегодня / today`, `завтра / tomorrow`, `послезавтра / day after
  tomorrow`.
- `к концу недели / end of [the] week` → Friday of the current week.
- `на этой неделе / this week` → same Friday.
- `на следующей неделе / next week` → upcoming Monday.
- `к концу месяца / end of [the] month` → last day of the current
  month.
- `к концу года / end of [the] year` → 31 December of the current
  year.
- `в этом месяце / this month` → last day of the current month.
- `в начале следующего месяца / beginning of next month` → 1st of
  next month.

**Weekday names** (Russian + English) with any optional preposition
(`к / до / на / ко / by / to / on / before`):
- `к пятнице`, `до понедельника`, `ко вторнику`, `by Friday`,
  `on Thursday`. Offset rolls forward when today is the same
  weekday.

**Day + month name** in both orders:
- Russian: `1 мая`, `до 5 июня`, `к 25 декабря`.
- English: `May 15`, `by Jun 15th`, `Apr 3rd`.
- Dates that already passed this year roll into next year.

**Bare month names** (no day) with a preposition:
- Russian: `к маю`, `в июне`, `до мая` → 1st of the next occurrence
  of that month.
- English: `by May`, `by June`.

**Through / in N units:**
- Russian: `через N день/дня/дней/недель/месяцев/лет`. Missing
  number means 1 (`через неделю` = +7 days).
- Russian `через пару дней / недель / месяцев` → N = 2.
- English: `in N day(s) / week(s) / month(s) / year(s)`, also
  `in a week / in an hour / a couple of weeks`.

**Not understood** (deliberately left null so the follow-up loop can
ask):
- `когда-нибудь`, `скоро`, `в ближайшее время`, `позже`,
  `some day`, `asap` — too vague to assign an ISO date.

#### FR-CR-04-4 — Focused owner prompt with conversation context

Owner extraction runs in its own LLM call with a minimal system prompt
that teaches only the assignee concept. The prompt explicitly lists
the author id (from `context.source_message.user`) and the 10 previous
messages, so the model can avoid picking the author or the audience.
A "no assignee" answer clears whatever the title pass might have
guessed.

#### FR-CR-04-5 — Date stripping in title / description

`strip_date_phrase(text)` removes the first date-like phrase from a
string (same patterns the resolver recognises, plus leading
prepositions). The pipeline pipes both the title and the description
through it, so date information lives **only** in `due_date`.

#### FR-CR-04-6 — Offer-first passive UX

Passive messages (no `@bot` mention) never auto-create a task.
Instead, `handle_message`:

1. Persists an `ActionDraft`.
2. Posts a pre-filled draft card in the source thread with three
   buttons: **Accept · Edit · Reject**.
3. Remembers `card_channel` / `card_ts` on the draft so Accept can
   morph the widget into a task card in place.
4. Asks the first missing field in the same thread via `prompt_for`,
   setting `draft.awaiting_field` so subsequent replies are consumed
   by `_handle_followup_reply`.

Accept → `finalize_draft` creates the task and morphs the widget; any
remaining gap triggers another follow-up question in the thread.
Edit → opens the task modal prefilled. Reject → draft marked
`ignored`, widget deleted.

#### FR-CR-04-7 — Quiet author fallback for the owner slot

When the LLM pipeline returns no explicit assignee,
`classify_and_persist` pre-fills the owner with the message author
and sets `TaskDraft.owner_assumed = True` on BOTH paths (mention +
passive). The card renders the owner as `<@author>
_(предположительно)_` so the user sees the implicit assignment; the
follow-up loop does NOT re-ask about the owner — pestering an author
who just wrote a task for themselves is noise. A user who needs to
reassign clicks **Edit** and picks a different owner in the modal;
that clears the `owner_assumed` flag.

This supersedes an earlier version of FR-CR-04-7 that asked about
the owner in chat whenever `owner_assumed` was true.

#### FR-CR-04-8 — Audio input via Whisper

Slack messages with `files: [{ mimetype: "audio/*" }]` are transcribed
before the pipeline sees the text:

- `extract_audio_files()` filters the audio attachments.
- `download_slack_file()` fetches each private URL with the bot token.
- `transcribe_bytes()` calls OpenAI `whisper-1` with a 25 MB per-file
  cap.
- `merge_transcripts_into_text()` preserves any typed caption and
  appends every transcript newline-separated.

Voice-only messages (empty `text`, at least one audio file) are NOT
filtered by `_is_ignorable`. Download / transcription failures are
best-effort: the typed caption still reaches the pipeline.

Bot requires the Slack scope `files:read` to fetch attachments.

#### FR-CR-04-9 — Prefilter safety net

A rule-based keyword prefilter
(`app/intent/rules.py: prefilter_intent`) complements the LLM
pipeline as a safety net for small models that occasionally return
`is_task=false` on unambiguous phrases. When the pipeline result is
`no_action` but the prefilter hint is `create_task` / `update_task`,
`classify_with_backend` synthesises a minimal draft from the source
text (with the date phrase stripped) at the prefilter's confidence.
Meeting hints (still recognised by `prefilter_intent` for back-compat)
do **not** trigger the override per FR-CR-04-19. The prefilter is no
longer used as a *gate*: the LLM pipeline runs on every passive message
when a backend is configured.

#### FR-CR-04-10 — Task-card Edit button

The post-confirm task card exposes an **Edit** button visible only to
the task owner and to admins. Click opens the task modal prefilled
with current values. Submit updates the fields, clears the
`owner_assumed` flag if present, and refreshes the card + DM mirror
via `refresh_task_card`. English labels throughout the task card
(`Start`, `Edit`, `Mark done`, `Subscribe`, `Unsubscribe`) replace
the earlier Russian strings to keep terminology consistent with the
buttons admins see on mobile.

### 10.3 Non-functional requirements

#### NFR-CR-04-1 — Per-stage resilience

A stage failing (exception, network, malformed output) does not abort
the whole pipeline. Stage 1 failures return `no_action`. Stage 2a/b
failures leave their fields null so the follow-up loop fills them
later. Stage 2c is deterministic and cannot fail.

#### NFR-CR-04-2 — Commit draft before finalize

`handle_message` snapshots everything it needs from the session
BEFORE leaving the `with session_scope()` block, then calls
`_always_create_and_admin_review` / the soft-prompt path only after
the transaction has committed. This prevents the
"`Draft N not found`" race where a nested `session_scope()` couldn't
see the uncommitted draft.

#### FR-CR-05-09 — Adaptive chat context, admin-owner fallback, source forwards

Quality follow-up to the first 100-message historical migration.
Three changes that together make confirm-first widgets actually
useful instead of a parade of low-signal drafts.

**1. Adaptive chat context.** Until now the classifier ran with
*zero* prior chat history for Telegram messages — every detect /
title / owner / date stage saw only the source line. Vague
acknowledgements like «хорошо! напишу ему» landed as the title
verbatim because there was nothing else to look at.

`TelegramSourceReader.recent_in_chat(chat_id, before_message_id,
max_chars=10_000, step=10, max_messages=50)` pulls prior messages
from the same chat, ordered newest-first by sent_at. It expands
in increments of `step` (10 → 20 → 30 …) until either the
combined `text` length crosses `max_chars` or we hit
`max_messages`. The list is reversed to chronological (oldest
first) before being handed to `ContextWindow.history_before`,
where the existing intent pipeline already consumes it.

The reader is wired through `TelegramIngestService.__init__(...,
reader=...)` and called from both the historical migration
(`ops.migrate_telegram_history`), the cron-driven incremental
ingest (`ops.telegram_ingest`), and the live listener
(`ops.telegram_listener`) — the listener uses the same Supabase
view because the Bot API itself doesn't ship history.

The detect prompt now treats parroted one-liners («ок, сделаю»,
«договорились», «хорошо! напишу ему») as no_action by default;
they only become tasks when `context` makes the work
unambiguous. The title prompt is taught to *rewrite* such
phrases into a proper imperative using the surrounding
conversation — «хорошо, напишу ему» with a prior «надо ответить
Андрею по сделке Acme» becomes title `написать Андрею по сделке
Acme`. Status-list reports («DBS — нет, Jefferies —
отправила, Stifel — не ответил») and OCR-noise singletons
(«файндхэзом») are also explicitly rejected upstream.

**2. Admin-owner fallback chain.** The author-fallback used to
land bot accounts as task owners — a forwarded post from a `bot`
user has `from.is_bot=true` and the bot's own user_id, so the
draft inherited that. The new chain is:

1. LLM-resolved owner — wins.
2. Sender, **only when** they're a registered chat member
   (FR-CR-05-07). A non-member sender is typically a bot account
   or a forwarded post; we don't promote them to owner.
3. First admin from `TELEGRAM_ADMIN_USER_IDS` — same identity the
   confirm-first widget already DMs by default. The admin's
   display name is read from the chat-members registry so the
   card shows «@andre_andreevich» rather than `222968032`.

This kills the «Валя is the owner because she was named in the
text but isn't in the table» class of bug — when the LLM
hallucinated an owner name that doesn't resolve to a real
user_id, the FR-CR-04-22 guard nulls it, and now the admin
fallback catches the gap instead of the message author bot.

**3. Inline-quote source fallback.** `post_draft_confirmation`
already tries `forwardMessage` first, but the Bot API only
forwards messages the bot has **observed via getUpdates**.
Historical migration drafts come out of the colleague's
read-only view, so the bot never saw them — every forward call
returns «message to forward not found» and the operator gets a
widget with no context. New code path: when forwardMessage
fails (sender returns `{}` or no `message_id`), the prepare-
drafts step pre-stashes `source_text` on `draft.payload[
"_pending"]`, and the card helper emits a
`<blockquote>`-wrapped HTML quote of the source so the operator
sees what triggered the widget without leaving the DM. Live
listener captures still get a real `forwardMessage` because the
bot did observe them — the fallback only fires when the forward
genuinely can't work.

#### FR-CR-05-08 — Task-card keyboard permission tightening

Per UX feedback the per-task buttons follow a strict role-based
visibility model:

- **▶ Start** — only the OWNER (assignee). Admins and bystanders
  see no Start button. An unowned task no longer surfaces Start to
  bystanders either; once an owner is set, that user gets the
  Start row, nobody else does.
- **✔ Mark done / ✏ Edit / 🗑 Delete** — OWNER or ADMIN.
- **🔔 Subscribe / 🔕 Unsubscribe** — anyone EXCEPT the OWNER.
  The owner is auto-subscribed at creation, so a Subscribe toggle
  for them would be a confusing no-op.

Layout: row 1 carries the primary action (Start / Mark done) when
visible, row 2 has Edit + Delete side-by-side, row 3 carries the
Subscribe toggle.

Pinned by `test_telegram_bot.py::test_task_card_keyboard_start_is_
owner_only` (covers owner / admin / bystander matrices for status
= todo).

#### FR-CR-05-07 — Telegram chat-members registry

The classifier was getting `known_employees=None` for every
Telegram message, which meant the LLM owner stage had nothing to
validate names against — natural mentions like «Валя сделай X»
landed as raw display strings, owners couldn't be DM'd directly,
and the FR-CR-04-22 hallucination guard had nothing to compare.

New table `telegram_chat_members` (migration `0016`) keyed by
`(chat_id, user_id)` records every user the listener has ever
seen speak in a given chat. Columns: `username` (nullable, the
@-handle), `first_name`, `last_name`, `has_started_bot`
(sticky-True flag set whenever we observe traffic in that user's
private chat with the bot — the only signal we have that
proves they're DM-able), and audit timestamps.

Pipeline:
- *Listener writes.* `TelegramListener.tick` calls
  `_upsert_member_from_update` after every parsed update. Service
  updates / callback queries with no `from` field are skipped.
- *Ingest reads.* `TelegramIngestService.process_all` /
  `prepare_drafts` build `known_employees` by calling
  `app.services.telegram_members.members_as_known_employees(
  chat_id)`. The shape mirrors what the Slack pipeline expects
  (`{slack_user_id, display_name, real_name}`); the field is
  named for legacy reasons but the classifier doesn't care about
  the prefix shape — for TG members we feed numeric user_ids.
- *Self-population.* The registry has no separate discovery RPC.
  Bot API admin enumeration only returns chat admins anyway, so
  we let real traffic populate the table — every user who has
  spoken in a chat the bot can see lands in the registry.

The new tests (`test_telegram_members.py`) pin: idempotent upsert
(same key → single row, profile fields don't blank out on a None);
`has_started_bot` sticky semantics; the `members_as_known_employees`
shape and per-chat isolation.

When dependent code is unavailable (a brand-new VM, a stale test
fixture without the migration), `_known_members_for` swallows the
import / query error and returns `[]` — the classifier just falls
through to no-known-employees mode, same as before.

#### FR-CR-05-06 — Dedup gate before widget + 10 000-char field cap

Two correctness gates added to the ingest pipeline so the user
isn't drowned in widgets and the DB / Sheet doesn't choke on
multi-MB strings.

**1. Dedup-before-draft.** New service
`app/services/task_dedup.py:check_duplicate(session, candidate,
llm_backend)` runs an LLM tool-call comparing the fresh
`TaskDraft` against the most-recent open Tasks (lookback = 20).
When the model says «duplicate», the candidate is silently dropped
in `TelegramIngestService.process_all` /
`prepare_drafts` — the source-message bookmark in
`processed_telegram_messages` is still written so a re-run of the
same view doesn't re-classify the dropped candidate.

Failure modes are conservative — empty lookback, no LLM backend,
or any LLM error → returns «not a duplicate» and the gate falls
open. The model occasionally hallucinates a task id outside the
lookback set; the service keeps the boolean verdict but nulls out
the id so callers don't dereference garbage.

The lookback excludes done / soft-deleted Tasks: closed work
shouldn't suppress a freshly-needed re-do.

**2. 10 000-char string cap.** Forwarded chat threads / pasted
documents can in principle blow past Sheets' 50 000-char cell
limit, inflate downstream LLM prompts, and bloat audit rows.
Every user-provided string field on `TaskDraft`
(`title` / `description` / `owner_user_id` / `owner_display_name`)
is now capped at 10 000 chars by a `model_validator(mode="after")`.
A belt-and-suspenders cap in `create_task_from_draft` catches any
raw-payload-dict path that bypasses the schema.

Both gates fire for every channel — Slack orchestrator, Telegram
immediate-create, the FR-CR-04-32 Accept-on-draft handler, and
the FR-CR-05-05 multi-task loop — because they live inside the
service / persistence layer rather than at the keyboard.

The historical migration (`ops.migrate_telegram_history`) and the
incremental cron (`ops.telegram_ingest`) inherit dedup for free
and now default to the *confirm-first* widget flow (FR-CR-04-32
parity): drafts go to the author / admins as «Create this task?»
DMs and the Task lands in the DB only after the user clicks ✅
Accept. The legacy «task straight to DB» path lives behind the
`--auto-confirm` flag for the rare case where you don't want to
click N buttons. The migrator also gained `--since YYYY-MM-DD` /
`--since-days N` so the operator can scope a backfill to «just
yesterday», bookmarking older messages as «skipped (too old)» so
they don't waste LLM budget on a re-run.

#### FR-CR-05-05 — Multi-task extraction from a single message

A single message often carries more than one task — «к завтра
сделать презу и отчёт к пятнице, плюс позвонить Васе сегодня» is
three separate work items, not one. The current pipeline only
returns one `TaskDraft` per message; this requirement extends it
to a list.

The pipeline change:

1. The detect stage now also produces a `task_count` hint and, for
   ``count > 1``, a list of disjoint source-text spans
   (``task_chunks``) — one chunk per task. The fallback when the
   LLM doesn't split is the whole message as one chunk (current
   behaviour).
2. Stages 2a (title / description / priority) and 2b (owner) run
   **once per chunk** instead of once per message; date resolution
   is also per chunk.
3. `IntentClassification` gains ``tasks: list[TaskDraft]`` (kept
   alongside the legacy ``task: TaskDraft | None`` shape, which
   becomes ``tasks[0]`` for back-compat with every existing call
   site).
4. Persistence iterates: `process_one` / `prepare_draft` / the
   confirm-first flow now create one `Task` (or one `ActionDraft`)
   per chunk. The captured-from card gets a multi-task header
   («✨ 3 new tasks from this message») and lists each numbered.

Edge cases:
- A user-targeted phrase that contains the word «и» but is really
  one task («сделать отчёт и презентацию по нему») must NOT be
  split. The detect prompt teaches this with explicit examples;
  the model is told to split only when each chunk has its own
  imperative verb / object pair.
- Mixed intents (one task + one meeting hint) — meeting hint
  becomes ``no_action`` per FR-CR-04-19; we never extract
  meetings. The task chunk continues normally.
- All chunks share the same source bookmark (chat_id, message_id);
  each chunk gets its own ``ActionDraft`` row but the
  ``processed_telegram_messages`` row points at the first Task
  (back-compat).

#### FR-CR-05-04 — Evening report (18:00 local): done today + subscriptions + tomorrow's plan

Replaces the old single-purpose «evening plan» DM. The 18:00 DM
now packs three sections in one message:

1. **«Done today»** — the user's own Tasks that flipped to
   ``done`` at any point during today (reads
   `task_status_history.changed_at` ≥ today midnight). One bullet
   per task with completion artifact when present.
2. **«Subscriptions update»** — every Task the user is subscribed
   to (excluding the ones they own) with its **current status** and
   any change *since the previous evening report*. We diff against
   the last evening DM's snapshot (stored in `audit_logs` under
   `category='evening_report'`).
3. **«Tomorrow's plan»** — the same auto-curated list of tomorrow's
   tasks that the previous «evening plan» message carried. Two
   buttons attach: ✅ Approve / ✏ Edit. Approve flips state to
   confirmed; Edit opens the same LLM-driven free-form reply that
   Edit-on-task uses (FR-CR-04-32). On no-input by 09:00 next day
   the plan auto-runs (FR-CR-04-25).

Slack / Telegram parity: the same routing rule (numeric uid → TG,
Slack uid → Slack) sends one DM per channel. Idempotency lives in
`audit_logs.category='evening_report'` keyed by `(user_id, date)`.

#### FR-CR-05-03 — «Task starting now» nudge

When a Task carries a `start_date` (and optionally `start_time`),
the bot DMs the owner a 2-line reminder *at* the start moment
(±5 min granularity). Subscribers get the same nudge in summary
form so a tracked task lighting up doesn't surprise them.

The cron tick runs every 5 minutes:

```
*/5 * * * *   python -m ops.send_digest --type starts-now
*/5 * * * *   python -m ops.telegram_digest --type starts-now
```

Each tick selects rows where ``start_date == today`` AND
``start_time`` is between ``now-5m`` and ``now``, joins the owner +
subscribers (de-duped), and DMs each. Idempotency: an
`audit_logs` row per (task_id, recipient_user_id, kind='start')
prevents repeats if a tick is replayed.

#### FR-CR-05-02 — Subscription updates throughout the day

The owner already gets DM'd on every status flip / Edit / Cancel
(FR-CR-04-12). FR-CR-05-02 closes the gap for **subscribers**:
every status transition (start / done / cancel / re-open) and
every Edit produces a one-line DM to each non-owner subscriber.

Implementation note: the change applies inside `TransitionService`
and `apply_edit_reply` rather than at the keyboard level — that
way the same subscriber-fanout fires regardless of the trigger
(Slack button, TG button, slash command, even a future API).
Per-recipient idempotency lives in `audit_logs` under
`category='subscriber_update'` keyed by
`(task_id, recipient_user_id, transition_id)`.

#### FR-CR-05-01 — Morning digest (08:00 local): today's tasks only

The 08:00 DM is now exactly *one* section: «Today's tasks»

The canonical time was nudged from 09:00 to 08:00 per UX feedback
— users want the day's plan in front of them BEFORE the work day
starts, not at the moment it starts.

The Today line dropped its noise: the previous
`*#42* title · status · due_date · priority · owner` collapsed to
`*title* · priority [· category · start HH:MM · due HH:MM]` —
`#id` is internal, owner is the recipient themselves, status is
either `todo` or `in_progress` (both mean «do it today»), and the
due_date repeats for every task in the section. The morning DM
now reads like a plain to-do list. The Slack and Telegram
renderers each got a dedicated `_today_*` helper so the noisy
multi-context formatter (`_tasks_mrkdwn` / `_fmt_task_line`)
keeps serving the evening report and watchlists unchanged.
— the owner's own Tasks with `due_date == today`, ordered by
`status` then `priority`. The previous combo of *Today /
Approaching / Overdue* moves to a separate optional weekly digest
(approaching) and to a per-task deadline reminder (overdue —
already handled by FR-CR-04-15).

Two buttons attach to the morning DM: 🔄 Refresh (re-fetches the
list — useful if a midnight-edited task changed status) and 📋
Show subscriptions (toggles a second message with the user's
subscribed tasks for context).

If the evening plan from the prior night was Approved, it pre-
populates today's order. If it was auto-run (no Approve by 09:00,
FR-CR-04-25), same.

Slack and TG share the same SQL: `_select_owner_today_tasks(uid)`
lives in the digest helpers and is called by both
`ops.send_digest` and `ops.telegram_digest`.

#### FR-CR-04-32 — Confirm-first widget for tasks captured in groups

Auto-creating a task from every task-shaped sentence in a group is
noisy and irreversible — the team wanted an explicit "create or
not?" step before the row lands in the DB. The Telegram listener
now splits the routing by chat type:

- **Private chat with the bot** (`chat.type == "private"`): the
  user is talking to the bot directly; consent is implicit. The
  flow stays *immediate-create* — `process_one` runs end-to-end
  and `post_initial_card` DMs the live card.
- **Group / supergroup / channel**: a new method
  `TelegramIngestService.prepare_draft` mirrors `process_one` up
  through the `ActionDraft` (state = `proposed`) but stops short
  of `create_task_from_draft`. The source / context-snapshot /
  fallback-author values are stashed under `payload["_pending"]`
  so the Accept handler can resume cleanly. The bot then DMs
  each recipient (author + admins; same set as FR-CR-04-31) a
  forwarded copy of the original message followed by a
  *"Создать задачу?"* widget with three inline buttons:

      [✅ Accept]   [✏ Edit]   [✖ Reject]

  Per-recipient `(chat_id, message_id)` pairs are recorded under
  `payload["_widgets"]` so any handler can edit every delivered
  copy when the draft resolves.

Click handling:
- **Accept** → `handle_confirm_draft` finalises the draft into a
  Task via the same `create_task_from_draft` helper used by the
  immediate-create path, points the
  `processed_telegram_messages` bookmark at the new Task id, and
  the listener calls `replace_widgets_with_task_card` which edits
  every widget DM into the regular task card with the
  `Start / Edit / Delete` keyboard. Idempotent — a second Accept
  on an already-confirmed draft just re-renders the card.
- **Reject** → `handle_ignore_draft` flips the draft to
  `ignored`, and `render_draft_rejected` swaps every widget into
  a `❌ Черновик #N — title — отклонён` tombstone with an empty
  keyboard.
- **Edit** on the widget is currently a friendly stub: "Accept
  first, then ✏ Edit on the task card". A `_looks_like_confirm_widget`
  helper inspects the click's `cq.message.reply_markup` to detect
  the confirm row pattern so the listener can route this path
  separately from a real task-edit click. Full draft-edit with
  LLM parsing is a follow-up.

Subtle correctness fix: `handle_confirm_draft` keeps `_widgets`
on the draft when popping `_pending` — otherwise
`replace_widgets_with_task_card` (called by the listener right
after) would find the widget list empty and silently no-op,
making Accept appear broken in the UI even though the Task was
created.

UX polish that landed alongside the confirm-first flow:
- Sender flipped from `parse_mode='Markdown'` (legacy) to
  `'HTML'` so usernames carrying underscores no longer trip the
  italic parser. `_escape_html` replaces `_escape_md`; bold uses
  `<b>…</b>`. The legacy alias is kept for back-compat.
- Status display: `in_progress` is rendered to users as
  `in progress` (underscore replaced with space).
- Owner display: `parse_update` now stores Telegram usernames
  with the leading `@` (e.g. `@andre_andreevich`). For old tasks
  whose `owner_display_name` was stored without `@`, a
  `_format_owner` heuristic prefixes one back at render time
  iff the value looks like a Telegram handle.
- Source permalink: the `https://t.me/c/<id>/<msg>` URL form
  only works for supergroups / channels (chat ids with the
  `-100` prefix). For basic groups it 404s with «no access»
  even for admins, so we now skip the link entirely for those.
- Edit-on-task prompt rewritten as a conversational form
  («Здесь уже есть: 📌 Title — …, 🟡 Priority — …», then
  «Не хватает: …», then a single-line "reply naturally" hint).
  Reply parsing routes through `parse_edit_with_llm` — the same
  intent backend that runs the classifier extracts structured
  field updates from free-form text. Pure `key=value` replies
  short-circuit the LLM call.
- Task card keyboard simplified: ⤺ Cancel removed (Edit + Delete
  cover the intent), leaving Start / Mark done / Edit / Delete /
  Subscribe-toggle.

#### FR-CR-04-31 — Telegram card privacy: DM author / owner / admins, never the group

The Telegram bot used to post the task card under the source
message in the chat where it was captured. In a group that means
*every member* sees the card — not the privacy model the team
wanted. New behaviour:

- The card is **never** posted in the source chat.
- One DM per recipient goes to:
  - the author of the source message (the user who wrote it);
  - the assignee, if the LLM resolved a different owner;
  - every Telegram admin from ``TELEGRAM_ADMIN_USER_IDS``.
- For a DM source the recipient set collapses to the user
  themselves and the card lands in the same DM the user wrote in
  — no behavioural difference vs. the old design.

Each delivered DM has its own `(chat_id, message_id)` pair. We
persist the full list on ``Task.extra["telegram_cards"]`` so a
status change can ``editMessageText`` every delivered copy.
``Task.card_channel`` / ``Task.card_ts`` keep pointing at the
first card for back-compat with the Slack-shaped fields.

The keyboard is rendered per-recipient from THEIR perspective
(`is_owner` / `is_admin`-aware), so the author, the owner and an
admin each see the buttons that make sense for their role —
exactly like Slack's "viewer" pattern on the task card.

Telegram quirk: the Bot API can DM only users who have already
started a private conversation with the bot (sent ``/start`` or
any DM). DMs to never-started users fail silently with a logged
`telegram_card_dm_failed` warning. Operator should ask team
members to ``/start`` the bot once.

Permissions enforcement is unchanged — the existing
`_ensure_can_edit` already gates Edit / Cancel / Delete /
Mark done by owner-or-admin. Bystanders (everyone else) simply
no longer see the card at all under FR-CR-04-31.

#### FR-CR-04-30 — TG ingest uses sender's user_name as fallback owner display

When the LLM owner stage runs without a `known_employees` table
(the typical Telegram path), it almost never extracts a display
name. The quiet-author-fallback then sets `owner_user_id` to the
sender's numeric Telegram id and leaves `owner_display_name`
empty, so the card and the Sheet end up showing "222968032"
instead of "Andre".

`TelegramIngestService.process_one` now copies `msg.user_name`
into `owner_display_name` when the LLM didn't provide one — but
**doesn't** overwrite an LLM-derived name, so explicit
"@Petya сделай X" still wins. The sheet renderer
(`_resolve_owner_name`) and the card text builder
(`build_task_card_text`) already prefer `owner_display_name`
over `owner_user_id`, so the fix takes effect everywhere
without further changes.

#### FR-CR-04-29 — Full Slack-parity for the Telegram bot

Closes the remaining gap between Slack and Telegram:

1. **TG admins** — `TELEGRAM_ADMIN_USER_IDS` env (comma-separated
   numeric Telegram user ids). Mirrors `ADMIN_SLACK_USER_IDS`.
   `_ensure_can_edit` now passes for owner OR admin.

2. **Mark done with optional artifact** via reply conversation.
   Click *Mark done* → bot posts a force-reply prompt (`Optional:
   reply with a link or short note. Or /skip.`) and registers a
   `PendingQuestion` in the listener's in-memory registry.
   When the user replies, `apply_done_artifact_reply`:
   - parses the text — URL prefix → `kind=url`, anything else →
     `kind=text`, `/skip` → no artifact;
   - persists `task.completion_artifact` + `_kind`;
   - transitions the task to done;
   - calls `sync_task` so Sheets updates.
   The card is then refreshed in place.

3. **Edit via key=value reply**. Click *Edit* → bot posts a
   force-reply prompt that includes the current values formatted
   as `title=...`, `description=...`, etc. The user replies with
   one or more `key=value` lines for fields they want to change.
   `parse_edit_payload` drops unknown keys; `apply_edit_reply`
   coerces dates / times / priority / etc and applies. Empty
   value clears the field. Invalid values (e.g. priority=critical)
   are silently ignored. `owner_assumed` is dropped on edit, same
   as the Slack flow.

4. **Pending registry** — `app/telegram_bot/pending.py`. In-memory
   `PendingRegistry` keyed by `(chat_id, user_id, prompt_message_id)`
   with a 10-minute TTL. Listener calls `register` after posting
   a prompt and `take` (atomic fetch+remove) when an inbound
   message has a matching `reply_to_message_id`. State is
   per-process; restart loses in-flight pendings — user just
   clicks again. Acceptable for the single-listener deployment.

5. **DM-based notifications for Telegram users** —
   `app/telegram_bot/notifications.py` mirrors the Slack
   notification surface for Telegram task owners and TG admins:
   - `send_morning_digest` — Today / Approaching (2 days) /
     Overdue per Telegram owner.
   - `send_evening_plan` — heads-up for tomorrow's plan,
     persists `daily_plan_items` so the morning execution path
     can read them. (Skip / Approve buttons are deferred — the
     Slack version's optional Approve already runs as-is in the
     morning per FR-CR-04-25, and the Telegram morning path
     mirrors that.)
   - `send_morning_plan` — today's tasks for Telegram owners.
   - `send_weekly_plan` — backlog for the upcoming week, sent
     Sunday evening.
   - `send_deadline_reminders` — DM the owner of any task with a
     due_date ≤ 2 days away (or already overdue).
   - `send_thread_reminders` — daily nudge in the source Telegram
     chat for each open Telegram-sourced task. Replies under the
     original message when we have its id.
   - `send_admin_watchlist` — DMs each TG admin with the team-
     wide *In progress* + *Overdue* lists.
   Recipients are filtered to numeric user ids only — Slack
   subscribers (`U…` / `W…`) never get a Telegram DM (and vice-
   versa). Per-user / per-day idempotency lives in `audit_logs`
   under category prefix `telegram_*` so Slack and Telegram
   digests don't shadow each other.

6. **Cron entry-point** — `python -m ops.telegram_digest --type
   <morning-digest|plan-evening|plan-morning|weekly|deadlines|
   thread-reminders|admin-watchlist>`. One-shot, idempotent,
   exits 0 on success. Operator runs the same schedule as the
   Slack cron, doubled with a Telegram entry per slot.

#### FR-CR-04-28 — Telegram task cards + button-driven lifecycle

Closes the loop on the Telegram channel: a task captured by the
ingest path (live listener or Supabase view) gets a card posted in
the source chat with the same buttons as the Slack task card, and
each button press drives the task through its lifecycle without
the user leaving Telegram.

What lands in the chat after ingest:

- A reply to the source message rendered by `build_task_card_text`
  — title, description, status / owner / priority / due meta line,
  source permalink (when public).
- An inline keyboard from `task_card_keyboard` — same conditional
  layout the Slack card uses:
  - *Start* on backlog/todo (owner or anyone if unowned);
  - *Mark done* on in_progress;
  - *Edit*, *Cancel* on every status except backlog (owner+admin);
  - *Subscribe* / *Unsubscribe* for bystanders;
  - *Delete* for owner+admin.

The bot stores the posted message's `(chat_id, message_id)` pair
on the task row using the existing `card_channel` / `card_ts`
columns (channel-agnostic; Slack reuses them too) so subsequent
updates target the same message via `editMessageText`.

Inbound flow (`callback_query` updates from button presses):

- The listener now subscribes to `callback_query` in addition to
  the four message kinds. Each press is parsed via
  `parse_callback_data` (`<action>:<entity_id>`) and dispatched to
  a Telegram-side handler in `app/telegram_bot/handlers.py`:
  `handle_start`, `handle_done`, `handle_cancel`, `handle_delete`,
  `handle_subscribe(subscribe=True/False)`, `handle_edit_help`.
- Handlers reuse the channel-agnostic services (`TransitionService`,
  `SubscriptionService`, soft-delete + audit log) and call
  `app.sync.task_sync.sync_task` afterwards so Sheets stays
  current.
- Permissions: owner-only for Mark done / Cancel / Delete /
  Edit; bystanders can subscribe / unsubscribe; an unowned task
  can be claimed by whoever clicks *Start* (mirrors Slack).
  `NotAuthorised` from a handler is surfaced as the
  `answerCallbackQuery` toast text so the user sees the rejection
  immediately.
- After a successful action the listener edits the original card
  in place via `app/telegram_bot/cards.refresh_card` (or
  `render_tombstone` on Delete) so the keyboard reflects the new
  state.

What's still deferred:

- *Mark done* opens a follow-up "reply with artifact" conversation
  rather than a modal. Not yet implemented; the MVP transitions
  with no artifact (matching FR-CR-04-21 — both fields optional).
- *Edit* posts a help message pointing the user at Slack for now.
  A Telegram-native edit conversation (`reply with title=...`,
  `due=...`) is the next iteration.
- DM-based digests / daily plan / reminders for Telegram users.
  Slack is the only DM target right now.
- TG admin support — currently only the task owner can do
  destructive actions. A `TELEGRAM_ADMIN_USER_IDS` env analogous
  to the Slack admin list will land alongside the digest work.

#### FR-CR-04-27 — Telegram live listener (Bot API long-polling)

A second ingest source for Telegram, parallel to the Supabase view
of FR-CR-04-26. The bot opens an outbound long-poll connection to
Telegram's Bot API (`getUpdates`) and processes each new message
through the same intent pipeline + same `processed_telegram_messages`
bookmarks + same `source_kind = 'telegram'` flag — so a message
captured by the live listener is **indistinguishable** in the DB
from one ingested via Supabase.

Why two paths:
- **Supabase view** (FR-CR-04-26) backfills the team's existing
  Telegram history and is the authoritative archive owned by the
  upstream pipeline.
- **Live listener** captures new messages instantly without waiting
  for the Supabase view to update, and works for chats / groups
  where the operator added the bot directly (no upstream pipeline
  needed).

The two paths race-process some new messages — that's fine. The
unique key on `processed_telegram_messages(chat_id, message_id)`
plus the bookmark check inside `process_one` make a duplicate a
no-op.

Layout (still strictly under `app/telegram_bot/` and `app/
telegram_ingest/`, no Slack code touched):

- `app/telegram_bot/listener.py`
  - `parse_update(update_dict)` maps a Bot API `Update` payload to
    the same `TelegramSourceMessage` shape `app/telegram_ingest/
    reader.py` uses for the Supabase rows. Picks the first
    message-shaped field present (`message`, `edited_message`,
    `channel_post`, `edited_channel_post`); returns None for
    service updates (callback queries, my_chat_member, etc).
  - `TelegramListener.tick()` — one long-poll cycle: read offset,
    call `getUpdates`, parse + process each message via the shared
    `TelegramIngestService.process_one`, advance the offset row.
    All inside a single `session_scope`.
  - `TelegramListener.run_forever()` — block-until-killed wrapper
    with a 1s sleep on idle and exception swallowing so transient
    failures don't crash the worker.
  - Outbound long-poll only — no public HTTP endpoint, no inbound
    port. The bot doesn't have to be reachable from the internet.

- `ops/telegram_listener.py` — daemon entry point. Run as a
  separate Docker container with `python -m ops.telegram_listener`.

Schema (migration `0015_telegram_listener_state`):

- `telegram_listener_state` — singleton row holding the highest
  `update_id` we've acked back to Telegram. Resume-from-this-offset
  on restart so we don't reprocess every update Telegram has
  retained in its 24h queue.

Operator setup:

1. Set `TELEGRAM_BOT_TOKEN` in the env file (already there if
   FR-CR-04-26 was done).
2. **Disable Group Privacy** in BotFather: `/mybots` → bot →
   *Bot Settings* → *Group Privacy* → *Turn off*. Otherwise the bot
   only sees `/commands` and direct mentions in group chats.
3. Add the bot to each chat / group that should be tracked.
4. Run as a sidecar container:
   ```
   docker run -d --name slack-task-tg-listener \
     --network slack-task-net --env-file /root/slack-task/.env \
     --restart unless-stopped \
     slack-task-bot:local \
     python -m ops.telegram_listener
   ```

Out of scope this iteration:

- Outbound replies to Telegram (the bot doesn't yet post draft
  cards back into the chat where the message originated; tasks
  land silently in the DB and the Sheet, same as FR-CR-04-26).
- Inline-button callback handler (the keyboards exist; the
  inbound dispatcher is the next iteration).
- Telegram-side digests / daily plan / reminders.

#### FR-CR-04-26 — Telegram channel as a second source

The bot grows a second input channel: Telegram. Tasks captured from
Telegram messages live in the **same** `tasks` table as Slack tasks
and show up in the **same** Google Sheet. A new `tasks.source_kind`
ENUM (`'slack'` | `'telegram'`) discriminates which channel a row
came from.

The Telegram source is exposed to us as a **read-only Supabase view**
(`humanoid_tg_chats_readonly`) that's populated by an upstream
ingestion pipeline outside our control. We never write to it; we
only page through new rows on a cron and feed each through the
existing intent pipeline.

Layout — strictly separate from Slack code:

- `app/telegram_ingest/` — view reader + ingest service.
  - `reader.TelegramSourceReader` opens a SQLAlchemy engine against
    `TELEGRAM_SOURCE_DATABASE_URL` with
    `default_transaction_read_only=on` and pages through the view in
    `(chat_id, message_id)` order. The mapping from view columns to
    our internal `TelegramSourceMessage` is loose — common name
    variants (`chat_id` / `chatid`, `message_id` / `messageid` / `id`,
    etc.) are accepted, so renaming a column upstream doesn't break
    the ingest.
  - `service.TelegramIngestService.process_one(session, msg)` wraps
    the message in a `ContextWindow` (using Telegram identifiers in
    Slack-shaped fields), runs `IntentClassifier.classify`, and on
    `create_task` creates an `ActionDraft` + immediately finalises
    a `Task` with `source_kind='telegram'`. Per FR-CR-04-19 the
    pipeline only emits `create_task`; meeting hints are still
    out of scope.
  - `process_batch` iterates a list and returns an `IngestReport`
    with per-outcome counters. Per-message errors are caught so one
    bad row never aborts the batch.
  - Idempotency lives in the new `processed_telegram_messages`
    table (PK = `(chat_id, message_id)`). Every processed message
    records a row, with `task_id` set when a task was created and
    NULL otherwise. `process_one` checks the bookmark first and
    no-ops on a hit.

- `app/telegram_bot/` — outbound surface.
  - `sender.TelegramSender` is a synchronous wrapper around the
    Bot HTTP API (stdlib `urllib`, no async runtime, no extra
    dependency on python-telegram-bot). Three methods —
    `send_message`, `update_message`, `delete_message` — plus
    `answer_callback_query`. Empty token disables the sender;
    every method then no-ops with a debug log line so calling code
    stays oblivious.
  - `keyboards.confirm_keyboard` / `keyboards.task_card_keyboard`
    build inline keyboards mirroring the Slack draft / task cards.
    Callback data uses a flat `<action>:<entity_id>` shape;
    `parse_callback_data` is the inverse for the inbound handler.
  - `sender.build_task_card_text` renders a `Task` as Markdown
    matching the Slack card visual order (title → meta line with
    status / owner / priority / due → permalink).

Ops:

- `python -m ops.telegram_ingest` — periodic cron (every few minutes).
  Resumes from the last `processed_telegram_messages` row and
  ingests one batch (`TELEGRAM_INGEST_BATCH_SIZE`, default 200).
- `python -m ops.migrate_telegram_history` — one-shot historical
  ingest. Walks the entire view start-to-finish, processes every
  message, persists tasks. Idempotent over re-runs (bookmarks).
  Supports `--dry-run`, `--limit`, `--batch-size`.

Schema layer (migration `0014_telegram_source`):

- `tasks.source_kind` enum column with default `'slack'`. Existing
  rows are backfilled to `'slack'` by the column default.
- `processed_telegram_messages` table — idempotency bookkeeping for
  the ingest worker. PK is `(chat_id, message_id)` (BigInteger);
  carries `processed_at` and an optional `task_id` FK.

Persistence:

- `create_task_from_draft(... source={...})` accepts an optional
  `source.kind` ('slack'|'telegram'). Slack call sites pass
  nothing → default `'slack'`. The Telegram ingest passes
  `kind='telegram'` → flag is set on the new row, and downstream
  Sheets sync renders the `source_permalink` column with a `t.me/c/`
  URL when the message lives in a public super-group.
- *Initial Sheets sync.* Every newly persisted Task triggers an
  `app.sync.task_sync.sync_task(task.id)` call from inside
  `create_task_from_draft` itself — not from the caller. Slack
  used to fire this from `FinalizeService._sync_task`, but the
  Telegram path skipped it (only later transitions synced), so
  TG-created tasks were missing from the Sheet until someone
  clicked Start. Centralising in the persistence layer makes
  every caller — Slack, Telegram immediate-create, the
  FR-CR-05-05 multi-task loop, the FR-CR-04-32 Accept-on-draft
  handler — get the sync for free. Best-effort: a Sheets outage
  must not abort task creation, so the call is wrapped in a
  bare `try/except` that swallows everything.

Sheet schema (one column per `_HEADER_ROW` entry in
`app/sync/sheets.py`):

- `task_id` / `title` / `description` / `owner` / `priority` /
  `category` / `start_date` / `start_time` / `due_date` /
  `due_time` / `is_recurring` / `recurring_weekdays` /
  `recurring_start_time` / `recurring_end_time` / `status` /
  `parent_task_id` / **`source`** ←*new* / `source_permalink` /
  `created_at` / `updated_at` / `deleted_at` /
  `completion_artifact`.

The `source` column shows the channel verbatim — `slack` or
`telegram` — so a glance at the spreadsheet reveals where each
task came from. Default is `slack` for any row whose
`tasks.source_kind` is unset (no migration needed; existing
Slack-only rows already carry the default).

Out of scope for this iteration (deferred):

- Telegram-side modal-equivalent UX for Edit / Mark done. The
  inline-keyboard buttons exist (`task_card_keyboard`); the inbound
  callback router that reacts to clicks is the next iteration.
- Telegram-side digests / daily plan / reminders. Telegram users
  don't yet receive evening / morning plan DMs; that's a planned
  follow-up.
- Telegram-side employees directory. The Slack `employees` table
  stays single-channel for now; Telegram messages carry the source
  user's id / name verbatim, and the owner of an auto-created
  Telegram task defaults to the message author.

#### FR-CR-04-25 — Daily plan: explicit Approve is optional

The evening DM still carries an *Approve plan* button, but it's now
explicitly **optional**. If the user goes to bed without clicking it
the morning run goes ahead anyway with whatever survived their
*Skip* clicks — no plan ever vanishes silently because the user
forgot one button.

Implementation lives in `app/services/daily_plan.py`:

- The evening DM context line reads
  *"Approve plan is optional — if you don't, we'll run this as-is in
  the morning."* — so the user knows the button isn't a gate.
- `handle_plan_approve` writes its `audit_logs` row in the same
  shape as `_mark_sent` (entity_id = plan_date ISO, actor = user_id)
  so the new `_was_explicitly_approved(user_id, plan_date)` helper
  can match by index without scanning JSON payload (avoids the
  Postgres `LIKE`-on-JSON gotcha noted in `_already_sent`).
- `send_morning_plan` calls `_was_explicitly_approved` first. When
  the user **didn't** click Approve, the bot:
  1. writes a `plan_auto_approved` audit row
     (category=`daily_plan`, action=`auto_approved`, actor=user,
     entity_id=plan_date) so the trail is explicit;
  2. passes `auto_approved=True` into `_morning_blocks` so the
     morning DM gets a small *":memo: Plan wasn't explicitly approved
     last night — running as-is."* note above the task cards.

The behaviour is symmetric for "approved": the morning DM looks the
same as before (no auto-approve note) and no `auto_approved` row is
written.

#### FR-CR-04-24 — Owner picker + sheet-side names from the employees table

A pair of QoL fixes that flow from the same observation: the bot already
has a fresh `employees` directory (FR-CR-04-12 / 17), so any UI surface
that lists / displays a person should read from it instead of relying
on the static `ALLOWED_OWNERS` env or whatever happens to be cached on
the `Task` row.

**Edit-modal owner picker.** `app/services/owners.py:list_known_owners`
queries `employees` (excluding bots), sorts by name, and falls back to
`ALLOWED_OWNERS` only when the table is empty. Wired into:
- `task_actions.handle_task_edit_open` (Edit on a confirmed task)
- `shortcuts.handle_shortcut` (Create task from message shortcut)
- `actions.handle_edit` (Edit on a draft card)

So newcomers appear in the dropdown without an env redeploy as soon as
the bot has seen them via workspace / per-channel sync.

**Sheets owner column.** `app/sync/sheets._resolve_owner_name` looks
up the owner via the same `employees` table and renders a
human-readable name. Order of preference:

1. `Employee.real_name` — Slack's `display_name` often falls back to
   the @username (e.g. `"admin"`), while `real_name_normalized`
   almost always carries the actual person's name. Real name first.
2. `Employee.display_name` — used when there's no real name.
3. `task.owner_display_name` with a leading `<@Uxxx>` Slack mention
   stripped to a bare uid (so the cell never shows raw mention syntax).
4. `task.owner_user_id` as a last resort.

The `list_known_owners` helper applies the same priority, so the Edit
dropdown and the spreadsheet stay consistent.

#### FR-CR-04-23 — Live Google Sheets sync (Service Account, all events)

The bot pushes the current state of every task to Google Sheets on
every change — not only at draft finalize. Four sub-changes:

1. **Service Account auth.** New env vars `GOOGLE_SERVICE_ACCOUNT_JSON`
   (inline JSON) or `GOOGLE_SERVICE_ACCOUNT_JSON_PATH` (file path).
   `app/sync/factories._resolve_credentials` prefers Service Account
   over the legacy OAuth-from-DB path; OAuth is kept as a fall-back so
   existing deploys don't regress. The Sheets / Tasks API scopes are
   bound to the SA at credential build time.
2. **Configurable tab name.** `GOOGLE_SHEETS_TAB_NAME`, default `Main`
   (was `Tasks`). Threaded through `build_sheets_factory` →
   `SheetsSyncService(sheet_name=…)`.
3. **`TaskSyncer` invoked on every mutation.** A new
   `app/sync/task_sync.py` module exposes a process-wide singleton:
   `set_active_syncer()` is called once at startup, `sync_task(id)` is
   a best-effort hook called from every handler that mutates a task —
   `_apply_transition` (Start), `handle_complete_task_submit`
   (Mark done), `handle_cancel_task`, `handle_delete_task_submit`,
   `handle_task_edit_submit`. Failures are logged, never raised, so
   Slack interactions don't break when Google is down.
4. **Auto-write header row.** `SheetsSyncService._ensure_headers`
   reads row 1 of the configured tab on the first sync per process.
   If it's empty or doesn't match `_HEADER_ROW`, the bot overwrites
   row 1 with its schema — column order in the sheet is now driven by
   the code, not by hand-typed headers that drift out of sync. One
   GET per process; flagged so subsequent syncs short-circuit.

Row schema (21 columns; the bot's order is the source of truth):

```
task_id, title, description, owner, priority, category,
start_date, start_time, due_date, due_time,
is_recurring, recurring_weekdays, recurring_start_time, recurring_end_time,
status, parent_task_id, source_permalink,
created_at, updated_at, deleted_at, completion_artifact
```

When `deleted_at` is set, the `status` column reads `deleted` (instead
of the underlying lifecycle status) so a glance at the sheet tells
the user what happened. The row stays in place for traceability.

Operator setup:

1. Create a Google Cloud Service Account; download its JSON key.
2. Enable the *Google Sheets API* (and *Google Tasks API* if you
   plan to keep Tasks sync alive) on the project.
3. Open the spreadsheet in Sheets and share it as **Editor** with
   the SA's email (looks like `something@project-id.iam.gserviceaccount.com`).
4. Set `GOOGLE_SHEETS_SPREADSHEET_ID` to the long ID from the
   spreadsheet URL, and `GOOGLE_SHEETS_TAB_NAME` to the tab name
   (defaults to `Main`).
5. Set `GOOGLE_SERVICE_ACCOUNT_JSON_PATH=/path/to/sa.json` (or paste
   the JSON inline into `GOOGLE_SERVICE_ACCOUNT_JSON=…`).
6. Restart the bot. The next task event appends/updates the row; the
   header row is written automatically on first sync.

#### FR-CR-04-22 — Per-channel employees sync + owner hallucination guard

Two coupled fixes for owner-attribution accuracy.

**(a) Per-channel sync.** `EmployeeDirectory.ensure_channel_synced`
walks `conversations.members` for the conversation the bot was just
addressed in and upserts every member through `observed()`. Called
from `classify_and_persist` for every passive / mention event. The
call is throttled in process memory: each channel hits Slack at most
once per `ttl_seconds` (default 1800). On bot restart the cache is
empty → first event in each channel re-syncs the roster, plugging
the long-standing gap where the directory only knew about people
who had already posted.

**(b) Owner hallucination guard.** `node_owner` was promoting the
LLM's `display_name` to a follow-up question even when the name
clearly came from the metadata header rather than the message text.
Two changes in `app/intent/pipeline.py`:

1. `resolve_owner_hint` candidates now include the employees'
   `real_name` as a second pseudo-row, so when the LLM emits the
   user's full real name (e.g. *"Андре Кузьминых"*) we still resolve
   it to the matching `slack_user_id`.
2. When the LLM emits an unresolvable name AND that name doesn't
   appear in the source / context AND the source author is in the
   employees table, drop the hallucinated `display_name`. The
   downstream "quiet author fallback" then quietly assigns the task
   to the author with `owner_assumed=True` instead of asking
   *"I couldn't find Андре Кузьминых in the list"*.

#### FR-CR-04-21 — Cancel + Delete on the task card; optional artifact

Three task-card / completion-modal changes per product feedback.

- **Cancel button.** Visible to owner and admins on every status
  except `backlog`. On click the task is routed to:
  - `todo`  — if the due date is within the current calendar week
    (Mon–Sun, today's local timezone);
  - `backlog` — otherwise (no due date, or due later than this week).
  Implemented as a normal `TransitionService.apply` so a status
  history row is written with `reason="cancelled"`. Subscribers are
  broadcast a status-change DM, and both the channel widget and the
  DM mirror are `chat.update`-d.
- **Delete button + confirmation modal.** Visible to owner and
  admins on every status. Opens a small confirmation modal
  (`MODAL_CALLBACK_DELETE_TASK`) that explains the action and asks
  the user to press *Delete* once more. Submit performs the soft
  delete (FR-CR-04-20) and replaces the channel widget + DM mirror
  with a tombstone context line — *":wastebasket: Task #N — title
  deleted by @actor"*.
- **Completion modal — both fields optional.** The `Mark done`
  modal previously enforced "at least one of link / description"
  with an explicit `response_action: errors`. That validation is
  removed: an empty submit is a valid completion. The artifact is
  only persisted when one of the inputs is non-empty.

#### FR-CR-04-20 — Four-state lifecycle + soft delete

Lifecycle simplification driven by product feedback ("ревью нет
статуса"):

- `TaskStatus` is reduced from five values to four:
  `backlog → todo → in_progress → done`. The `review` value is
  dropped from the Python enum. Migration `0013_soft_delete_drop_review`
  data-migrates any task stuck in `review` to `in_progress` and, on
  Postgres, recreates `task_status` without the value (the standard
  rename / create / cast / drop dance — `ALTER TYPE … REMOVE VALUE`
  doesn't exist).
- `ALLOWED_TRANSITIONS` is rewritten so every cross-state move is
  legal except a self-loop. This is what lets *Cancel* drop a task
  to `todo` / `backlog` from any state without complicated
  exception paths. Self-loops still raise `InvalidTransition`.
- `tasks.deleted_at: datetime | None` (indexed). Every query that
  feeds the UI, digests, daily / weekly plans, workload estimator,
  and thread reminders adds `Task.deleted_at IS NULL`. A soft-deleted
  task is hidden from every view but its row stays in the DB
  alongside an `audit_logs` row of category `task` / action
  `task_deleted` so we can trace deletions.
- `Submit for review` button + `handle_submit_review` handler +
  `ACTION_SUBMIT_REVIEW` constant are removed. The post-confirm
  task card now offers `Start → Mark done` plus the cross-cutting
  `Cancel` and `Delete` from FR-CR-04-21.

#### FR-CR-04-19 — Meetings out of scope

Per product direction the bot is now task-only. Meeting capture and
the meeting modal are disabled at runtime; the schema layer
(`MeetingDraft`, `IntentType.create_meeting/update_meeting`,
`meetings` table, `app/persistence/meetings.py`,
`bk.meeting_modal`, `bk.draft_card`'s meeting branch) is left alone
so historical drafts / audit rows stay parseable and the rollback
path is one revert away.

Behavioural guarantees (each is a test in `test_meetings_disabled.py`):

- **Classifier prefilter override** (`app/intent/classifier.py`) only
  synthesises drafts for task hints. A meeting keyword in the source
  text — even a strong one — leaves the classification at `no_action`
  rather than promoting it to `create_meeting`.
- **`create_meeting_from_message` shortcut**
  (`app/slack_bot/handlers/shortcuts.py`) is still registered (legacy
  app manifests reference it) but no longer opens a meeting modal.
  Instead it `chat.postEphemeral`s a polite "this bot only handles
  tasks now — try *Create task*" notice in the source channel,
  addressed to the invoking user.
- **The pipeline itself** never emits `create_meeting`: `node_detect`
  classifies tasks vs. not-tasks; there is no separate meeting branch.

The `IntentType.create_meeting` / `update_meeting` enum members and the
`MeetingDraft` Pydantic schema are kept so old `IntentInference.raw`
rows keep parsing. Test coverage for the dead user-facing surfaces
(meeting modal field-by-field, draft-card meeting rendering, soft-
prompt meeting copy, follow-up question ordering for meetings,
finalize-meeting persistence) was deleted along with this change —
those tests were exercising paths no user can reach.

#### FR-CR-04-18 — Modal cleanup + coloured priority

The Edit / Create modal lost two redundant blocks per user
feedback:

- **Recurring checkbox** removed. Selecting any weekday in the
  multi-select IS the toggle now: `is_recurring = bool(weekdays)`.
- **"Estimated effort (min)"** removed. The `tasks.estimated_minutes`
  column stays in the schema for back-compat / future analytics, but
  the modal no longer surfaces it and `_extract_task_payload`
  always emits `estimated_minutes=None`.

Priority gets coloured circle emoji both in the modal's
`static_select` options and on the task-card meta line:

```
low      :large_green_circle:  Low
medium   :large_yellow_circle: Medium
high     :large_orange_circle: High
urgent   :red_circle:          Urgent
```

The mapping lives in `blocks.PRIORITY_EMOJI` so any future addition
to `TaskPriority` requires registering a colour (covered by the
`test_priority_emoji_dict_covers_every_priority_value` regression
test).

#### FR-CR-04-17 — Always-fresh Employees directory

The owner LLM stage receives `known_employees` from the local
`employees` table (FR-CR-04-12). To keep that table complete the
bot now keeps it in sync with Slack via three mechanisms:

1. **Bot startup** — after `build_app` constructs the Slack client,
   `app.main:run` calls
   `EmployeeDirectory.sync_workspace_members(session)` which walks
   `users.list` (paginated) and upserts every workspace member.
   Bot accounts are stored with `is_bot=True`; `USLACKBOT` is
   skipped. A Slack failure here is logged and ignored — startup
   never blocks on it.
2. **`team_join` event** — when a new person joins the workspace,
   we trigger a single-user `observed()` upsert.
3. **`member_joined_channel` event** — when the bot itself is the
   joiner, we walk `conversations.members` for that channel and
   refresh every roster member; when someone else joins, we call
   `observed()` for that user.

Manual backfill: `python -m ops.sync_employees` runs the workspace
sync from the command line; useful right after a fresh deploy or
when permissions on `users.list` have just been granted.

Required Slack scopes (Bot Token):
- `users:read` — `users.list`, `users.info`.
- `users:read.email` — optional, fills `Employee.email`.
- `channels:read` / `groups:read` / `mpim:read` / `im:read` —
  `conversations.members` per the channel kind the bot lives in.

#### FR-CR-04-16 — Bot UI is English

All user-visible bot strings are in English: button labels, modal
titles and labels, follow-up questions, ack messages, draft-card
hints, completion-modal copy, thread reminders, weekly / daily plan
prompts, subscription modal, soft prompts. Examples include:
- "Captured: *<title>*."
- "What's the deadline?", "Who's the assignee?"
- "All filled. *Accept* and the task ships to the tracker."
- "Plan for YYYY-MM-DD", "Today — YYYY-MM-DD"
- "Approve plan", "Skip", "Accept", "Later"
- "Tracking (N)", "Manage subscriptions", "Subscriptions", "Done"
- ":repeat: Mon/Wed 09:00–11:30"
- "(implicit)" suffix on owner when assumed.

**Russian still appears** — and intentionally — in three places:
1. **LLM prompt examples** (detect/title/owner/date prompts in
   `app/intent/*.py`). Russian examples teach the small models to
   recognise Russian phrasing like "к понедельнику" / "ко
   вторнику". Removing them would degrade extraction quality on
   Russian-speaking teams.
2. **Regex / lookup tables** parsing Russian user input — weekday
   stems, prepositions, month-name genitives, "через N
   <unit>" patterns in `date_resolver.py`, `followup.py`,
   `rules.py`. These never reach the user; they parse text the user
   wrote.
3. **Source-code comments** describing pipeline behaviour. They are
   developer-facing and not part of any user output.

#### FR-CR-04-15 — Recurring tasks

A task can repeat on selected weekdays during an optional time
window. Four columns on `tasks`:

- `is_recurring BOOLEAN` (master switch).
- `recurring_weekdays JSON` — list of "mon" / "tue" / … / "sun".
- `recurring_start_time Time | NULL`.
- `recurring_end_time Time | NULL`.

Edit modal: a "Recurring" checkbox plus three optional blocks
(weekdays multi-select, start/end timepickers). They live in the
modal at all times — UX discipline: tick the checkbox AND pick at
least one weekday or the recurring schedule is treated as not set.

Submit logic: `is_recurring = (checkbox AND weekdays non-empty)`.
When falsy, all four columns are cleared so leftover modal values
never persist quietly. The admin-edit handler captures the same
fields in its diff audit row.

Card render: shown as a meta segment — `:repeat: Mon/Wed
09:00–11:30` when the time range is set, `:repeat: Mon/Wed` when
just weekdays.

Sheets sync gains four columns: `is_recurring` (yes / blank),
`recurring_weekdays` (comma-joined), `recurring_start_time`,
`recurring_end_time`.

Migration: `0012_task_recurring`. No engine change yet — calendar
booking will read these in a future iteration.

#### FR-CR-04-14 — Daily plan workflow (evening approval + morning execution)

Two cron jobs per user per day cover the daily routine:

**Evening (default 18:00 London) — `python -m ops.send_digest --type
plan-evening`.** For each user, the bot:
1. Picks "candidate" tasks for *tomorrow*: tasks they own, not done,
   matching at least one of: `start_date == tomorrow`,
   `due_date == tomorrow`, OR (`is_current_week` AND status in
   {todo, in_progress}).
2. Inserts a row into `daily_plan_items` per candidate task. Rows
   are unique per `(user_id, plan_date, task_id)`.
3. DMs the user a single message with one *Skip* button per task
   plus a single *Принять план* button at the bottom. Below the
   plan — a Tracking section listing every task they're subscribed
   to (the "favourites").

The user clicks *Skip* on tasks they won't do tomorrow. Each click
sets `daily_plan_items.excluded_at` and writes an audit row. *Принять
план* is symbolic — the plan is whatever rows are still
non-excluded; the click just records explicit confirmation.

**Morning (default 09:00 London next day) — `python -m
ops.send_digest --type plan-morning`.** Reads `daily_plan_items`
where `excluded_at IS NULL` for today and DMs the user one full
`task_card` per surviving task (so the existing *Start* / *Mark
done* / *Edit* buttons all work straight from the morning DM).
Tracking section appears below.

Idempotency — both jobs check `audit_logs` for
`(category=daily_plan, action={evening_sent,morning_sent},
actor=user_id, payload.plan_date=YYYY-MM-DD)` and short-circuit on
re-run. So cron retries / double-fires don't duplicate DMs.

Schema:
- migration `0011_daily_plan_items` adds the
  `daily_plan_items(id, user_id, plan_date, task_id, excluded_at,
  created_at)` table with `UNIQUE(user_id, plan_date, task_id)` and
  `INDEX(user_id, plan_date)`.

#### FR-CR-04-13 — Start time, category, subtasks

Three additional fields on `tasks`, all optional, all settable via
the Edit modal (no UI in the LLM pipeline — these are not derived
from message text):

- `start_date` (Date) + `start_time` (Time) — when the assignee
  plans to start the work. Reserved for a future calendar-booking
  integration; right now they're shown on the task card as
  `start: YYYY-MM-DD HH:MM` and synced to Google Sheets.
- `category` (String 64) — free-form direction label
  ("маркетинг" / "разработка" / "ops" / …). String not enum so
  adding a new bucket doesn't need a migration.
- `parent_task_id` (Integer FK → tasks.id, ON DELETE SET NULL) —
  one-parent-per-child subtask hierarchy. SQLAlchemy
  `Task.subtasks` (children) and `Task.parent` (back-ref). Deleting
  a parent leaves children with `parent_task_id = NULL` rather
  than cascading.

Migration `0010_task_extras` adds the columns and indexes
`ix_tasks_parent_task_id`, `ix_tasks_category`. Sheets sync row
header gains `category`, `start_date`, `start_time`,
`parent_task_id` columns; the diff audit row in admin-edit
captures changes to all three.

#### FR-CR-04-12 — Employees table in the owner prompt

The owner LLM stage receives a structured `known_employees` table —
one row per known team member from the `employees` directory, with
`slack_user_id`, `display_name`, `real_name`. The prompt instructs
the model to pick a `slack_user_id` from this table whenever the
message names someone, instead of returning a bare display_name.

After the LLM returns:
- An `slack_user_id` that's NOT in the table is dropped (anti-
  hallucination guard); we keep only the display_name.
- A bare `display_name` is run through `resolve_owner_hint` against
  the table to fill in the matching `slack_user_id` deterministically.

This fixes the "bot assigns to author when another teammate was
named" bug: in a group DM with Иван + Паша + bot, "Иван, сделай X"
now lands on Иван's `slack_user_id` instead of falling back to the
speaker.

The author-fallback in `classify_and_persist` only fires when BOTH
`owner_user_id` AND `owner_display_name` are null after the LLM
stage — i.e. the message didn't name anyone at all. When a name is
present but unresolved, the bot asks the user to clarify in chat
instead of silently picking the wrong person.

#### FR-CR-04-11 — Full message archive

Every Slack event the bot receives is persisted verbatim into a
new audit table `slack_events_archive` BEFORE any
`_is_ignorable` filtering. The table has no unique constraint, so
edits (`message_changed`), deletes (`message_deleted`), system
notices (`channel_join`, `bot_message`) and replays each get their
own row. Columns: `event_id`, `event_type`, `subtype`,
`conversation_id`, `ts`, `thread_ts`, `user_id`, `text`,
`transcript`, `raw` (full event JSON), `received_at`. Archive
writes are best-effort: a failure logs and continues, never blocks
event handling.

The "useful" `slack_messages` table now also stores:
- `subtype` — original event subtype if any.
- `transcript` — newline-joined Whisper transcripts of audio
  attachments on this message.
- `has_audio` — boolean, true when the event carried an audio
  file (even if Whisper failed).
- `raw` — the full Slack event payload (was nullable & unused).

### 10.4 Data model delta

Migration `0008_archive_and_transcript`:
- New table `slack_events_archive`.
- New columns on `slack_messages`: `subtype`, `transcript`,
  `has_audio`.

CR-04 also reuses:

- `action_drafts.card_channel / card_ts / awaiting_field` (from CR-02)
  to morph widgets on Accept.
- `tasks.extra.owner_assumed` (JSON flag, set by
  `create_task_from_draft`) to drive the owner follow-up question.

### 10.5 Acceptance criteria

- Passive "надо подготовить заметки к 1 мая" → draft card with
  `title = подготовить заметки`, `due_date = 2026-05-01`, owner empty
  + "Кому назначаем?" asked in thread. Accept creates the task.
- Mention "@бот надо сделать X" without an explicit assignee →
  task created, owner = author `(предположительно)`, bot asks "Кому
  назначаем?" in the source thread. Reply "Иван" updates the owner
  and clears the label.
- Voice note (no caption) → Whisper transcript is used as the source
  text. Typed caption + voice note → both reach the pipeline.
- Pipeline returns `no_action` on non-task chat without running the
  Stage-2 LLM calls.

## 11. Requirements → tests traceability

One requirement may have several tests. Tests not listed here either
cover multiple requirements listed in their module docstring or are
pure unit tests for internal helpers.

### Base SPEC

| ID    | Test modules                                      |
|-------|---------------------------------------------------|
| FR-1  | `test_fr_01_05_ingestion.py`                      |
| FR-2  | `test_fr_01_05_ingestion.py`                      |
| FR-3  | `test_fr_01_05_ingestion.py`                      |
| FR-4  | `test_fr_01_05_ingestion.py`                      |
| FR-5  | `test_fr_01_05_ingestion.py`                      |
| FR-6  | `test_fr_06_10_explicit.py`                       |
| FR-7  | `test_fr_06_10_explicit.py`                       |
| FR-8  | `test_fr_06_10_explicit.py`                       |
| FR-9  | `test_fr_06_10_explicit.py`                       |
| FR-10 | `test_fr_06_10_explicit.py`                       |
| FR-11 | `test_fr_11_12_persistence.py`                    |
| FR-12 | `test_fr_11_12_persistence.py`                    |
| NFR-1  | `test_nfr_01_05.py`                              |
| NFR-2  | `test_nfr_01_05.py`                              |
| NFR-3  | `test_nfr_01_05.py`                              |
| NFR-4  | `test_nfr_01_05.py` (passive-never-auto-creates) |
| NFR-5  | `test_nfr_01_05.py`                              |
| NFR-6  | `test_nfr_06_11.py`                              |
| NFR-7  | `test_nfr_06_11.py`                              |
| NFR-8  | `test_nfr_06_11.py`                              |
| NFR-9  | `test_nfr_06_11.py`                              |
| NFR-10 | `test_nfr_06_11.py`                              |
| NFR-11 | `test_nfr_06_11.py`                              |

### CR-01

| ID        | Test modules                                    |
|-----------|--------------------------------------------------|
| FR-CR-1   | `test_cr01_owners_workload.py`                   |
| FR-CR-2   | `test_cr01_owners_workload.py`                   |
| FR-CR-3   | `test_cr01_lifecycle.py`                         |
| FR-CR-4   | `test_cr01_handlers.py`, `test_cr01_lifecycle.py`|
| FR-CR-5   | `test_cr01_handlers.py`, `test_subscribe_widget_toggle.py` |
| FR-CR-6   | `test_cr01_digest.py`                            |
| FR-CR-7   | `test_cr01_lifecycle.py`, `test_cr01_handlers.py`|
| NFR-CR-1  | `test_cr01_nfr.py`                               |
| NFR-CR-2  | `test_cr01_nfr.py`                               |
| NFR-CR-3  | `test_cr01_nfr.py`                               |

### CR-02

| ID           | Test modules                                    |
|--------------|--------------------------------------------------|
| FR-CR-02-1   | `test_mention_always_replies.py`, `test_mention_fallback_always_creates.py`, `test_mention_fallback_date.py` |
| FR-CR-02-2   | `test_followup_flow.py`, `test_cr02_spec_coverage.py`     |
| FR-CR-02-3   | `test_followup_flow.py`, `test_multi_field_reply.py`, `test_widget_morph_and_dm.py` |
| FR-CR-02-4   | `test_draft_card_lifecycle.py`                  |
| FR-CR-02-5   | `test_cr01_lifecycle.py` (no-open-source test)  |
| FR-CR-02-6   | `test_daily_digest_tracking.py`                 |
| FR-CR-02-7   | `test_daily_digest_tracking.py`                 |
| NFR-CR-02-1  | `test_mention_dedup_upsert.py`                  |
| NFR-CR-02-2  | `test_mention_always_replies.py`                |

### CR-03 (current scope — admin tooling kept, always-create/admin-review reverted)

| ID           | Test modules                                           |
|--------------|--------------------------------------------------------|
| FR-CR-03-1   | `test_cr03_employees_admin.py`                          |
| FR-CR-03-2   | `test_cr03_employees_admin.py`                          |
| FR-CR-03-3   | *reverted — see FR-CR-04-6 + `test_passive_draft_card.py`, `test_cr03_mention_vs_passive.py`, `test_cr03_admin_review.py`* |
| FR-CR-03-4   | *reverted — see FR-CR-04-6 + same tests as above*       |
| FR-CR-03-5   | `test_task_edit_button.py`, `test_cr03_admin_review.py`, `test_widget_morph_and_dm.py` |
| FR-CR-03-6   | `test_cr03_weekly_plan.py`                              |
| FR-CR-03-7   | `test_cr03_completion_artifact.py`, `test_cr01_handlers.py` |
| FR-CR-03-8   | `test_cr03_admin_digest.py`                             |
| FR-CR-03-9   | `test_cr03_thread_reminders.py`                         |
| FR-CR-03-10  | `test_widget_morph_and_dm.py`, `test_dm_threading.py`   |
| NFR-CR-03-1  | `test_cr03_employees_admin.py`                          |
| NFR-CR-03-2  | `test_cr03_admin_review.py`                             |
| NFR-CR-03-3  | `test_cr03_admin_digest.py`                             |
| NFR-CR-03-4  | `test_cr03_thread_reminders.py`                         |
| NFR-CR-03-5  | `test_cr03_admin_review.py`                             |

### CR-04

| ID           | Test modules                                                                                                 |
|--------------|--------------------------------------------------------------------------------------------------------------|
| FR-CR-04-1   | `test_intent_pipeline.py`, `test_intent_graph.py` (detect node + routing)                                    |
| FR-CR-04-2   | `test_intent_pipeline.py`, `test_intent_graph.py` (parallel fan-out)                                         |
| FR-CR-04-3   | `test_date_resolver.py`, `test_mention_fallback_date.py`, `test_intent_graph.py` (LLM-first date + Python validator) |
| FR-CR-04-4   | `test_owner_focused_prompt.py`                                                                               |
| FR-CR-04-5   | `test_date_resolver.py` (strip_date_phrase), `test_intent_pipeline.py`                                       |
| FR-CR-04-6   | `test_passive_draft_card.py`, `test_cr03_mention_vs_passive.py`, `test_mention_passive_symmetry.py`, `test_cr03_admin_review.py` (negative: passive no auto-create) |
| FR-CR-04-7   | `test_mention_follow_up_for_assumed_owner.py`, `test_mention_passive_symmetry.py`                            |
| FR-CR-04-8   | `test_audio_transcription.py`                                                                                |
| FR-CR-04-9   | `test_prefilter_override.py`, `test_passive_pipeline_runs_always.py`                                         |
| FR-CR-04-10  | `test_task_edit_button.py`                                                                                   |
| FR-CR-04-11  | `test_message_archive.py` (slack_events_archive + raw / transcript / has_audio on slack_messages)             |
| FR-CR-04-12  | `test_owner_employees_table.py` (employees table in owner prompt; id validation; name → id resolution)        |
| FR-CR-04-13  | `test_task_extras.py` (start_date/start_time, category, subtasks via parent_task_id)                          |
| FR-CR-04-14  | `test_daily_plan.py` (evening approval card with Skip/Approve, morning execution card, idempotency, tracking) |
| FR-CR-04-15  | `test_task_recurring.py` (recurring checkbox, weekdays, optional time range, card render)                     |
| FR-CR-04-16  | English UI strings across the bot (assertions in many test modules; specifically `test_units_support.py::test_soft_prompt_*`, `test_mention_always_replies.py`, `test_passive_draft_card.py`, `test_daily_plan.py`, `test_cr03_thread_reminders.py`) |
| FR-CR-04-17  | `test_employees_workspace_sync.py` (sync_workspace_members + sync_channel_members + bot startup hook) |
| FR-CR-04-18  | `test_priority_emoji.py` (modal cleanup: no recurring checkbox / no effort block, weekdays-as-toggle, coloured priority emoji on options + card meta) |
| FR-CR-04-19  | `test_meetings_disabled.py` (prefilter never synthesises a meeting draft, meeting shortcut posts ephemeral "tasks-only" notice, task shortcut still opens the task modal); also `test_prefilter_override.py::test_prefilter_does_not_override_for_meeting_keywords`, `test_fr_06_10_explicit.py::test_fr10_meeting_shortcut_shows_disabled_notice` |
| FR-CR-04-20  | `test_cr01_lifecycle.py` (four-state enum, allowed transitions graph, retired values rejected), `test_task_cancel_delete.py` (review enum value gone, soft-delete excluded from workload + daily plan), migration `0013_soft_delete_drop_review.py` |
| FR-CR-04-21  | `test_task_cancel_delete.py` (Cancel routing — within-week → todo, later → backlog; owner+admin only; Delete confirmation modal; soft-delete with audit row; tombstone card refresh; completion modal accepts an empty form) |
| FR-CR-04-22  | `test_owner_hallucination_guard.py` (real-name match against the employees table; drop unresolvable-and-not-in-source name so quiet-author-fallback fires), `test_employees_per_channel_sync.py` (`ensure_channel_synced` upserts roster, throttles repeats, isolates channels) |
| FR-CR-04-23  | `test_sheets_sync_hooks.py` (Service-Account preferred, OAuth fall-back; configurable tab name; `_task_row` flips status to `deleted` when soft-deleted; TaskSyncer no-op without factory; Cancel / Delete / Start handlers call the active syncer; `_ensure_headers` writes / overwrites / no-ops correctly and runs at most once per process); plus updated `test_sync_factories.py` |
| FR-CR-04-24  | `test_owners_from_employees.py` (`list_known_owners`: real_name first, display_name fallback, env fallback when DB empty, bots excluded, classic "admin" → real-name case); `test_sheets_sync_hooks.py::test_owner_resolves_to_real_name_via_employees`, `::test_owner_strips_slack_mention_when_employee_unknown`, `::test_owner_uses_display_name_when_no_real_name`, `::test_owner_returns_owner_user_id_as_last_resort` |
| FR-CR-04-25  | `test_daily_plan.py::test_morning_runs_without_explicit_approve`, `::test_morning_writes_auto_approved_audit_when_no_approve`, `::test_morning_dm_shows_auto_approve_note_when_no_approve`, `::test_morning_skips_auto_approve_when_user_clicked_approve`, `::test_evening_card_copy_says_approve_is_optional` |
| FR-CR-04-26  | `test_task_source_kind.py` (default slack, telegram persisted, enum coverage, `create_task_from_draft` honours `source.kind='telegram'`); `test_telegram_ingest.py` (reader maps canonical and alternative column names, drops orphans, no-op when unconfigured; `_telegram_permalink` for super-group / private; `_build_window` shape; `process_one` creates Task with source_kind=telegram + bookmark; records no_action without creating a task; idempotent on repeat; skips empty text without invoking classifier; batch counters per outcome; psycopg2→psycopg3 scheme rewrite; IPv4 hostaddr injection); `test_telegram_bot.py` (confirm + task-card keyboards, callback round-trip, card text rendering, sender disabled when token empty); migration `0014_telegram_source.py` |
| FR-CR-04-27  | `test_telegram_listener.py` (`parse_update` for message / edited_message / channel_post; caption fallback; first+last name composition; service updates dropped; tick processes updates and advances offset; no_action bookmark without task; non-message updates skipped; second tick with same offset is a no-op; disabled when token empty); migration `0015_telegram_listener_state.py` |
| FR-CR-04-28  | `test_telegram_handlers.py` (Start owner / unowned-claim / stranger-rejected; Done owner-only; Cancel routes by due_date; Delete soft-deletes + audit row + via=telegram + stranger-blocked; Subscribe/Unsubscribe for bystanders, owner is no-op; Edit help text; `_route_on_cancel`); `test_telegram_listener.py::test_listener_tick_routes_callback_query_to_handler` |
| FR-CR-04-29  | `test_telegram_conversations.py` (PendingRegistry register / take / TTL eviction / no-match guards; `prompt_done` + `apply_done_artifact_reply` for /skip / URL / text; `prompt_edit` includes current values; `parse_edit_payload` filters unknown keys + handles empty values; `apply_edit_reply` flips fields, clears on empty value, silently ignores invalid priority, drops `owner_assumed`, blocks stranger; `admin_user_ids` env parsing; admin can edit, non-admin/non-owner blocked); `test_telegram_notifications.py` (numeric-uid filter, owner ids list, morning digest content + idempotency + skip-empty, evening plan persists items, morning plan needs seeded items, deadline reminder per-day dedup, thread reminders post to source chat with reply_to, admin watch-list DMs each admin, no-admins is a no-op) |
| FR-CR-04-30  | `test_telegram_ingest.py::test_process_one_uses_user_name_as_fallback_owner_display_name`, `::test_process_one_keeps_llm_display_name_when_present` |
| FR-CR-04-31  | `test_telegram_cards.py` (TG-uid filter; recipient set order author → owner → admins, dedup when author == owner, Slack uids dropped; `post_initial_card` sends one DM per recipient, persists `extra["telegram_cards"]` + back-compat `card_channel`/`card_ts`, no `reply_to_message_id` forwarded, no-op when no recipients or task is Slack-sourced; `refresh_card` iterates every stored card; legacy single-pair fallback; `render_tombstone` updates every card with empty keyboard) |
| FR-CR-04-32  | `test_telegram_listener.py::test_listener_routes_group_messages_to_draft_flow` (group → ActionDraft state=proposed, `_widgets` + `_pending` stashed in payload; no Task yet), `::test_listener_confirm_button_finalises_draft_into_task` (Accept finalises, draft.state=confirmed, widget chat/message edited in place — guards against the popped-`_widgets` regression), `::test_listener_reject_button_marks_draft_ignored`, `::test_listener_at_mention_in_group_skips_confirm_widget` (explicit @ → immediate-create); `test_telegram_bot.py` (HTML-mode card text renders underscored usernames literally, escapes `<`/`>`/`&`, status with space, Edit + Delete share a row, Cancel never appears); `test_telegram_conversations.py::test_parse_edit_with_llm_*` + Edit-on-draft suite (`prompt_edit_draft` lists filled / missing fields and blocks strangers; `apply_edit_draft_reply` updates payload; LLM-silent fallback returns empty applied); pending registry lenient match for force-reply ignored; `test_telegram_ingest.py::test_process_one_uses_user_name_as_fallback_owner_display_name` also asserts `owner_user_id` is set from `message.user_id` so the keyboard's `is_owner` check matches |
| FR-CR-05-01  | `test_cr01_digest.py::test_daily_digest_lists_today_only` (Slack: morning blocks contain today's task only — Approaching/Overdue suppressed); `test_telegram_notifications.py::test_morning_digest_today_only` + `::test_morning_digest_today_renders_optional_fields` (TG: minimal Today renderer — no `#id` / owner / status / per-task date; optional description / category / start / due time surface when present) |
| FR-CR-05-02  | `test_subscriber_updates.py` (owner is excluded; non-owner Slack + numeric TG subscribers each receive one DM, routed by uid shape; replay of the same transition is idempotent; unknown uid shapes are dropped; `TransitionService.apply` triggers the fanout via the active dispatcher with zero caller boilerplate; `dispatch_status_change` is a no-op when no dispatcher is set; autouse fixture clears the singleton across suites) |
| FR-CR-05-03  | `test_telegram_notifications.py::test_starts_now_dms_owner_when_start_time_is_now` (5-min `[now-5m, now]` window; tasks scheduled hours later are skipped; per-`(task, recipient, kind=start)` idempotency); `Slack::DigestKind.starts_now` shares the selection logic via `DigestService._starts_now` |
| FR-CR-05-04  | `test_telegram_notifications.py::test_evening_plan_includes_three_sections` (Done today + Subscriptions update + Tomorrow's plan in one DM; `_done_today_for_owner` uses `task_status_history.changed_at` ≥ today midnight; `_subscribed_open_for_recipient` excludes the recipient's own tasks; auto-run by 09:00 next day inherited from FR-CR-04-25) |
| FR-CR-05-05  | `test_telegram_ingest.py::test_process_all_creates_one_task_per_chunk` (`tasks=[a, b]` ⇒ 2 Task rows; bookmark points at the first); `::test_prepare_drafts_creates_one_draft_per_chunk` (group multi-task ⇒ 2 ActionDrafts in proposed; each carries its own `_pending`); `test_intent_pipeline.py::test_detect_prompt_asks_single_yes_no_question` updated for the new `task_count` / `task_chunks` schema |
| FR-CR-05-06  | `test_task_dedup.py` (empty lookback short-circuits; missing backend falls open; LLM «duplicate» propagates with verified id; LLM-invented task id is nulled; LLM error is swallowed; done / soft-deleted tasks excluded from lookback; only open tasks reach the prompt); `test_units_support.py::test_task_draft_truncates_long_strings_to_10k` + `::test_task_draft_short_strings_pass_through` (schema cap); `test_telegram_ingest.py` integration paths exercise the gate via `_make_service` fakes |
| FR-CR-05-07  | `test_telegram_members.py` (idempotent upsert, profile fields don't blank out on a None, `has_started_bot` stickiness, `members_as_known_employees` shape with @username / first+last / numeric-id fallback, per-chat isolation); migration `0016_telegram_chat_members.py`; listener-side write covered by the existing `test_telegram_listener.py` flows that exercise `_upsert_member_from_update` via `parse_update` (no separate test — the upsert is wrapped in a try/except so a missing migration in fixture mode never breaks ingest) |
| FR-CR-05-08  | `test_telegram_bot.py::test_task_card_keyboard_start_is_owner_only` (Start visible only to owner; admin sees Edit/Delete + Subscribe but no Start; bystander sees only Subscribe), `::test_task_card_keyboard_for_owner_in_progress_shows_done_edit_cancel_delete`, `::test_task_card_keyboard_for_bystander_shows_subscribe_only`, `::test_task_card_keyboard_subscribe_toggles_to_unsubscribe`, `::test_task_card_keyboard_done_status_collapses_to_delete_only`, `::test_task_card_keyboard_does_not_show_cancel_anywhere`, `::test_task_card_keyboard_edit_and_delete_share_a_row` |
| FR-CR-05-09  | `test_telegram_ingest.py::test_build_window_carries_history_before` (history_before threads through to the ContextWindow); `::test_process_all_falls_back_to_admin_when_owner_unresolved` (no LLM owner + sender is a non-member ⇒ owner = first admin from `TELEGRAM_ADMIN_USER_IDS`); `::test_process_all_keeps_real_member_sender_as_owner` (sender registered in chat-members ⇒ author fallback wins, no admin promotion); `::test_prepare_drafts_stashes_source_text_for_quote_fallback` (`_pending["source_text"]` populated for the inline-quote fallback); `test_telegram_cards.py::test_post_draft_confirmation_uses_inline_quote_when_forward_fails` (forwardMessage returns `{}` ⇒ a `<blockquote>`-wrapped HTML quote is sent before the widget); `test_intent_pipeline.py::test_detect_prompt_lists_status_reports_and_parroted_phrases_as_no_action` + `::test_title_prompt_teaches_imperative_rewrite_from_context` (prompt content pinned) |
| NFR-CR-04-1  | `test_intent_pipeline.py` (stage-failure tests), `test_intent_graph.py` (per-node failure isolation), `test_owner_focused_prompt.py` (owner-stage failure) |
| NFR-CR-04-2  | `test_nfr_01_05.py` (`test_nfr2_dedup_retry_from_slack_does_not_post_new_card`)                              |
