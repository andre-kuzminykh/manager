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

#### FR-CR-05-59 — Fireflies docs shared as anyone-with-link writer

`DocsExportService.export_summary` now defaults to creating
the doc in the Shared Drive folder AND immediately calling
`drive.permissions().create({"type": "anyone", "role":
"writer"})` so the doc URL embedded in the short Telegram
summary is openable + editable by every teammate without a
per-person share dance. Pass `share_role=None` to skip.

`supportsAllDrives=True` is mandatory on the permissions
call — without it Drive treats the file as personal-Drive
and 404s. Same flag added to `_create_doc_in_folder` and
`_move_to_folder` (FR-CR-05-56).

#### FR-CR-05-58 — Fireflies-extracted tasks post TG cards

Operator: «почему задачи из этой встречи не вычленяются,
сначала саммери с гиперссылкой, а потом список задач».

Pre-fix: Fireflies tasks landed in DB + Sheet but no TG
card was posted, so the operator only saw them next morning
in the digest. The «summary first, then tasks» UX needed
the tasks visible inline in the bot DM straight after the
short summary.

Fix: in `_step_extract_tasks` after each successful Task
insert, call `post_initial_card(sender, session, task,
…)`. The helper's `source_kind == telegram` early-return
was relaxed to `source_kind == slack` (skip Slack only) so
Fireflies tasks now flow through the TG card path. Author
attribution = the admin uid (so the card's button-permission
model still works).

Order of TG messages per Fireflies meeting becomes:
  1. Short summary DM (with Google Doc URL)
  2. One task card per extracted task (with Start / Edit /
     Mark done / Subscribe keyboard)

#### FR-CR-05-57 — Fireflies task extraction: role/notes routing

Operator: «4 задачи на Кузьминых стоят если тот Lead AI,
проверь что используются роли и ноутс в контексте для
выбора ответственного».

Root cause: `TASK_EXTRACTION_SYSTEM` only referenced
FR-CR-05-31 (role/notes) and FR-CR-05-52 (assistant routing)
by NAME, didn't inline the rules. The Fireflies LLM call
saw `known_employees` with role/notes columns but had no
explicit guidance to:

  - Route work to assistants when principal's notes say so
    («только стратегические задачи», «assistant: Ирина»);
  - NEVER pick the «Lead AI» row for routine business work;
  - Treat the speaker (e.g. Артём talking through next
    steps) as DELEGATING, not assigning to themselves.

Net result was every routine task in a meeting Артём ran
fell back to the admin (Кузьминых = «Lead AI»). New
TASK_EXTRACTION_SYSTEM ships with all four rules inlined +
a worked example: routine prep work mentioning Артём → goes
to Ирина (his assistant), NOT Андрей (Lead AI).

#### FR-CR-05-56 — Drive API supportsAllDrives parameter

When the operator's Workspace uses Shared Drives, every
Drive API call that touches Shared-Drive content needs
`supportsAllDrives=True` — without it Drive treats the
folder ID as a personal-Drive ID and 404s. Added to
`_create_doc_in_folder`, `_move_to_folder`, and the
permissions-share call from FR-CR-05-59.

#### FR-CR-05-55 — Fireflies docs created inside the folder, not SA's Drive

Service accounts in non-Workspace projects have ZERO
personal Drive storage quota — `documents.create` 403s
with «caller does not have permission». Workaround:
`drive.files().create({mimeType: 'application/vnd.google-
apps.document', parents: [folder]})` lands the doc in the
folder directly. When the folder lives in a Shared Drive
(operator action: create one in Workspace, share with SA
as Content manager), the doc is owned by the drive — pooled
storage, no per-SA quota.

`export_summary` branches: `parent_folder_id` set →
create-in-folder via Drive API; empty → legacy
`documents.create` (still works for Workspace SAs / OAuth
user creds). `_move_to_folder` no longer called from the
create-in-folder branch — doc is born in the right place.

#### FR-CR-05-54 — Fireflies short summary: bigger + participants

Operator: «короткое саммери побольше сделай и список
участников укажи из встречи прям». `SHORT_SUMMARY_SYSTEM`
target raised to 1500-3500 chars (hard cap 3800 — under
Telegram's 4096 limit), new mandatory `👥 Участники`
section listing every meeting attendee, optional
«💬 Главные обсуждения» topic-by-topic block.
`_step_short_summary` passes `participants` as a structured
prompt block and bumps `_truncate` cap from 2000 → 3800.

#### FR-CR-05-53 — Fireflies pipeline retries failed steps

The early-return in `process_one` checked only
`processed_at AND tasks_extracted`. After Docs API was
disabled and `_step_doc_export` 403d, the rest of the
pipeline still ran and `processed_at` was set — so a
retry skipped the failed step instead of fixing it. The
check now requires EVERY per-step flag, so partially-failed
runs DO retry the failed step on the next pass.

#### FR-CR-05-112 — Drop LLM-picked due_date when source has no temporal anchor

Operator regression: «🟡 Поставить встречу по Бете с
Джарадом / 📅 2026-05-05» — the date is the «вторник» from
«понедельник или вторник» in the description, but that's
the MEETING SLOT, not the task's deadline. Operator policy
(FR-CR-05-89/-94): «либо в описание добавляй пруф либо
сегодня».

`pipeline.py::node_date` adds a Python post-LLM gate: when
the source text doesn't contain an explicit temporal anchor
(«к понедельнику» / «до 5 мая» / «by Friday» / «дедлайн» /
«завтра» / «сегодня»), drop the LLM-extracted due_date.
Downstream then defaults to «today 18:00» per FR-CR-05-63.

Implementation:
  - `_TEMPORAL_ANCHOR_RE` — regex over Russian + English
    deadline phrases. Captures: «к + day/month», «до + …»,
    «by + …», «before + …», «дедлайн», «крайний срок»,
    «срок до», «deadline», «due by/on», «tomorrow»,
    «завтра», «сегодня», «послезавтра».
  - `_has_explicit_temporal_anchor(source_text)` — returns
    True iff the regex matches.
  - When False, `node_date` logs `date_node_dropped_no_
    temporal_anchor`, sets `rejected=<llm_iso>`, and
    `picked=None`.

The LLM-correctly-null + intentional-reasoning path
(FR-CR-05-101) still wins over the Python fallback. The
new check fires AFTER LLM-picked-date is parsed but
BEFORE the source_used flag is decided.

#### FR-CR-05-111 — Description similarity safety net under LLM dedup

Operator regression: «Поставить встречу по Бете с Джарадом»
vs «Назначить встречу по Бете с Джарадом» landed as 2 tasks.
Different titles (synonym verbs «поставить» / «назначить»)
so FR-CR-05-110 exact-title path didn't fire. But descriptions
were ≈ 95% identical (same Zoom ID 91444990696, same password
197584, same 13:00-13:45 slot). LLM dedup still missed.

`task_dedup.py` adds a SECOND tier in the `dedup_fast_path`
block:

  1. Exact-title + owner overlap (FR-CR-05-110) — fires first.
  2. NEW: Description `SequenceMatcher` ratio ≥ 0.70 + owner
     overlap (FR-CR-05-111). Catches the «synonym title +
     near-identical description» case the LLM keeps missing.

Implementation:
  - `_description_similarity(a, b)` — `difflib.SequenceMatcher`
    ratio over lowercased + whitespace-collapsed inputs.
  - `_similar_description_owner_match(candidate, existing,
    threshold=0.70)` — return first existing item whose
    owner-key overlaps AND similarity ≥ threshold. Skips
    items with descriptions <50 chars.
  - On match, `is_duplicate=True`, reason mentions «description
    similarity ≥0.70», LLM call skipped. Logged as
    `task_dedup_similar_description_match`.
  - Threshold 0.70 tuned conservative.

#### FR-CR-05-110 — Narrow exact-title + owner safety net under LLM dedup

Operator: «слушай, дубль 1 в 1, это из-за чего». Even gpt-5.5
LLM-only dedup keeps missing identical-title + same-owner
duplicates (Atuwatse Okorodudu × 2, PALADIN Goldman Sachs ×
2, Mohammad Farhan × 2). Operator earlier rejected
deterministic dedup, but empirically LLM-only ships dups.

`check_duplicate` now runs `_exact_title_owner_match` BEFORE
the LLM call:
  - normalised title (lowercase + whitespace-collapse +
    punctuation-strip + ё→е) equality
  - candidate's owner-key set INTERSECTS existing's
    (uid OR display_name on either side)

Match → `is_duplicate=True`, LLM not called. Operator can
disable via `DEDUP_FAST_PATH=0` env var (new
`Settings.dedup_fast_path` config field).

#### FR-CR-05-109 — Reflection / observation rejection; resolve uid → «Name (uid)» in pipeline context

Two operator regressions in one commit:

  1. «🟡 Только я не понял, как будто мы с ними, они не
     поняли нашу ситуацию» — author's reflection /
     confusion, not a delegation. Should be is_task=false.
  2. Operator: «97239970 — имена подтягивай сразу в
     контекст вместе с цифрой». LLM saw bare numeric
     Telegram uids in source/context and couldn't tie them
     to people; routing decisions suffered.

Fixes:

  - **`_TRANSCRIPT_PREFIX_RE` extended** with reflection /
    observation patterns: «Только я не понял», «я не
    понимаю / уверен», «мне кажется / показалось»,
    «возможно,», «странно, что», «интересно, что»,
    «видимо,»; English: «I don't / didn't understand»,
    «I'm not sure», «I think we / they / maybe»,
    «It seems like / that». Forces is_task=false at the
    Python pre-detect guard.

  - **`_annotate_uids` + updated `_resolve_uids_in_text`.**
    `prepare_drafts` now passes the source message and
    every history-before entry through uid resolution
    BEFORE building the context window for the classifier.
    Bare numeric uids that have a `team_members` row land
    in the prompt as «Andrей Кузьминых (97239970)» — both
    the human-readable name AND the original uid. The LLM
    can route correctly; the operator can grep logs by uid.

#### FR-CR-05-108 — Python pre-detect guard for transcript-prefix sources; remaining fallback removal; owner-stage diagnostic logging

Three regressions arrived together:

  1. «🟡 На изображении показано сообщение от Chris Doran»
     landed as a task despite FR-CR-05-89's TRANSCRIPTION
     DUMP detect rule. gpt-5.5 keeps marking these as
     is_task=true. Same pattern hit «На изображении
     показано электронное письмо…» and «Обсуждают сообщения
     внутри группы…» in earlier rounds.
  2. «📝 обсуждалось в Юля - аналитик · 2026-04-30 14:55»
     fallback descriptions still appearing — FR-CR-05-105
     removed only ONE of THREE call sites that applied
     `_fallback_description`; the other two were untouched.
  3. «🟡 Согласовать письмо с Артемом / 👤 Артем Соколов»
     — owner picked the message author instead of his
     assistant Ирина. Operator: «проверь как вызывается
     поиск контакта».

Three fixes:

  - **`_looks_like_transcript_dump(source)` Python pre-LLM
    guard in `node_detect`.** When source matches the
    `_TRANSCRIPT_PREFIX_RE` regex (Russian: «На изображени*»,
    «На скрин*», «На фото», «Обсужда*т», «В переписк*»,
    «В треде», «В диалог*», «В чате», «По переписк*», «По
    обсуждени*», «Сообщени* от ...»; English: «In the
    image / screenshot / chat / thread», «This image /
    screenshot shows / depicts», «On the screen»),
    is_task=false is forced WITHOUT calling the LLM.
    Operator told us not to add Python checks; this is a
    deliberate exception after 3 hours of the same
    regression slipping through prompt-only fixes.

  - **Removed the two leftover `fallback_desc` call sites**
    in `app/telegram_ingest/service.py` (FR-CR-05-105
    completed the third). Drafts whose description came
    back empty are now uniformly DROPPED at flush instead
    of having «обсуждалось в <chat> · <date>» templated
    in.

  - **Owner-stage diagnostic logging.** `pipeline.py::
    node_owner` now logs a per-call summary line tagged
    `owner_node_result` with: candidate count, candidate
    ids, candidate display↦role pairs (top 20), the LLM's
    raw `slack_user_id` / `display_name` / `reasoning`
    pick, and the post-validation `final_uid` /
    `final_name`. Operator can grep this in container logs
    to diagnose mis-attributions (e.g. when the owner
    prompt picks the author instead of the assistant
    despite FR-CR-05-52/77/79 routing).

#### FR-CR-05-107 — gpt-5.5 also rejects `temperature=0`

Operator deployed FR-CR-05-106 fix and date_node hit a new
400: «Unsupported value: 'temperature' does not support 0
with this model. Only the default (1) value is supported.».
gpt-5.x / o-series reasoning models accept ONLY the
default temperature (1).

`OpenAIBackend.call_tool` and `OpenAIBackend.complete_text`
now:
  - Omit the `temperature` kwarg entirely when
    `_model_uses_completion_tokens(model)` is True
    (server uses default 1).
  - Keep `temperature=0` (call_tool) and
    `temperature=temperature` (complete_text) for
    gpt-4o-family.
  - On 400 «temperature ... unsupported», retry once
    without the kwarg as a safety net.

#### FR-CR-05-106 — gpt-5.5 wants `max_completion_tokens`, not `max_tokens`

Operator deployed gpt-5.5 (FR-CR-05-104) and the date_node
call returned 400 «Unsupported parameter: 'max_tokens' is
not supported with this model. Use 'max_completion_tokens'
instead.». gpt-5.x / o1 / o3 / o4 (the «reasoning» family)
renamed the output-cap kwarg.

`app/intent/llm_backends.py::OpenAIBackend.call_tool` now:
  - Calls `_model_uses_completion_tokens(model)` to pick the
    right kwarg name per model. Returns True for any model
    name starting with `gpt-5`, `o1`, `o3`, `o4`; False
    otherwise (gpt-4o, gpt-4-turbo, gpt-3.5-turbo).
  - On a 400 «unsupported_parameter» error mentioning the
    OTHER kwarg name, retries once swapping `max_tokens` ↔
    `max_completion_tokens` so the call survives even if our
    static mapping ever lags behind OpenAI's deprecations.

Anthropic backend unchanged — Anthropic SDK still uses
`max_tokens` everywhere.

#### FR-CR-05-105 — Pure-LLM pipeline; drop drafts the LLM can't describe

Operator (third hour, exhausted): «убери нахуй все
детерменированные штуки, оставь только LLM, но в контекст
бери по задаче 10 сообщений чтобы определить задача это или
нет и контекст к ней для описания и бери 10 предыдущих задач
чтобы понять дубль это или нет!!! Если не задача — сразу
отбрасывай. Если задача — оформи как все, после чего проверь
на дубли. Если есть дубли — не выводи».

Two structural changes in `app/telegram_ingest/service.py:
prepare_drafts`:

  - **Stripped out the Python title post-processors that
    were masking LLM behaviour:**
      - `strip_chat_prefix_to_imperative` (FR-CR-05-103)
      - `strip_first_person_prefix` (FR-CR-05-101)
      - `is_naked_verb_title` (FR-CR-05-99)
    The detect prompt + title prompt + gpt-5.5 model handle
    all of these patterns directly. Only `normalize_task_
    title` (≤80-char hard cap + first-letter capitalize)
    survives — it's a cheap rendering safety net, not an
    LLM-impersonation.

  - **Drop drafts whose description is empty.** The
    deterministic «обсуждалось в <chat> · <date>»
    fallback is GONE. Operator: «такой формат сука» —
    those rows clutter the backlog with task-less
    placeholders the operator can't act on. Instead, when
    the LLM returns no description, the draft is deleted
    from the session and never reaches the widget queue.
    The detect prompt should already mark such messages
    `is_task=false` (FR-CR-05-104 chat-opener block); the
    description gate is the second-line defence for cases
    detect lets through.

Pipeline final shape (per operator's spec):
  1. Take message + ≤10 prior messages from same chat
     (`_adaptive_context_for`).
  2. LLM detect → if `is_task=false`, drop.
  3. LLM title + description from the 10-message context.
     If LLM gave no description → drop.
  4. LLM dedup (gpt-5.5) against ≤10 most recent open
     tasks/drafts with their full descriptions. If
     duplicate → drop.
  5. Ship the widget.

No Python checks between LLM calls.

#### FR-CR-05-104 — Chat-opener + retrospective recap rejection; gpt-5.5 everywhere

Operator regression: «Смотри, по GP Morgan, я вчера с Артёмом
просто переписывалась, я у него…» landed as a task with
verbatim title and a fallback description. Plus operator
direction: «давай поставим gpt-5.5 везде».

Two fixes:

  - **`detect_prompt.py` — Chat-opener + retrospective recap
    block.** Sentences starting with conversational fillers
    («Смотри, ...», «слушай, ...», «короче, ...», «вот,
    ...», «эй, ...», «look, ...», «hey, ...») followed by
    past-tense recap of a prior conversation («я вчера с X
    переписывалась», «мы обсудили», «говорил с Y») are
    chat, not delegations — even when they trail off with
    «…» (the dots are conversational, not a hidden
    imperative). is_task=false. The exact GP Morgan
    regression pinned as a worked counter-example.

  - **All model defaults bumped to `gpt-5.5`.** Operator-
    cited release: <https://openai.com/index/introducing-
    gpt-5-5/>. Six fields in `app/config.py`:
      `openai_model` — gpt-4o-mini → gpt-5.5
      `openai_date_model` — gpt-4o → gpt-5.5
      `openai_dedup_model` — gpt-4o → gpt-5.5
      `fireflies_summary_model` — gpt-4o → gpt-5.5
      `fireflies_short_summary_model` — gpt-4o-mini → gpt-5.5
      `fireflies_tasks_model` — gpt-4o-mini → gpt-5.5
    Whisper transcription model unchanged. Operators whose
    key doesn't have GPT-5.5 access yet override via the
    matching env vars (`OPENAI_MODEL=gpt-4o` etc.).

#### FR-CR-05-103 — Strip @-mention + politeness wrappers from chat-question titles

Operator: «"@IrinaMorato подскажи, пожалуйста, отправить
фоллоу-ап Neuberger ?" — да как блять такое происходит, почему
такой формат сука!!! Здесь должно быть название задачи что
сделать, далее описание более подробное».

Source is a chat question pointed at someone — starts with
`@handle`, contains a politeness verb («подскажи»), ends
with `?`. The action verb («отправить фоллоу-ап Neuberger»)
is buried in the middle. The LLM kept emitting the source
verbatim as the title.

Two-layer fix:

  - **Python post-process `strip_chat_prefix_to_imperative`**
    in `app/persistence/tasks.py`. Runs in `prepare_drafts`
    BEFORE `strip_first_person_prefix` / naked-verb check /
    `normalize_task_title`. Strips, in order:
      1. Leading `@handle,?` mentions (one or more).
      2. Politeness verb + comma: «подскажи / скажи /
         напомни / уточни / расскажи / помоги / ответь /
         реши»; «tell me / remind me / let me know / help
         me».
      3. Standalone «пожалуйста» / «please» softener (with
         or without trailing comma).
      4. Trailing `? ! . , ; :` punctuation.
    Result is capitalised. If after stripping the title is
    a naked verb («подскажи» on its own), the downstream
    `is_naked_verb_title` check drops the draft.

  - **Title prompt — CHAT-QUESTION REQUESTS block.** Teaches
    the LLM to recognise the pattern and emit the imperative
    plus a description naming WHO asked WHOM about WHAT. The
    Neuberger regression pinned with «Отправить фоллоу-ап
    Neuberger» as the worked rewrite + description shape
    «Игорь спрашивает, нужно ли отправить … Обсуждается в
    чате CEO Office».

#### FR-CR-05-102 — Dedup call upgraded to gpt-4o (away from gpt-4o-mini)

Operator: «надо смотреть в описание и с LLM сравнивать как я
написал: берем последнее сообщение и 10 последних задач и
спрашиваем LLM есть ли дубли — все!!!». No deterministic
matching. Pure LLM dispatch.

Root cause of the persistent dup regressions (Atuwatse
Okorodudu × 2 with literally-identical titles AND
near-identical descriptions; PALADIN Goldman Sachs × 2):
the dedup LLM call was using `openai_model` (gpt-4o-mini)
which is unreliable on long-form Russian description
comparison. The model defaults to «not duplicate» on
near-misses.

Fix: `task_dedup.py::check_duplicate` now forces the dedup
call to use `gpt-4o` via the new
`Settings.openai_dedup_model` config (defaults to `gpt-4o`,
override with `OPENAI_DEDUP_MODEL=…`, set empty string to
fall back to `openai_model`). Same trick the date node
already uses (`openai_date_model`). The model override is
passed via the existing `call_tool(model=...)` kwarg;
backends without that kwarg silently use their default.

Architectural unchanged from operator's spec:
  - take new task (full title + description)
  - take 10 recent open tasks/drafts (full title +
    description, ≤1500 chars each)
  - LLM verdict: yes → drop draft; no → ship.
  - no Python title/owner/date matching.

#### FR-CR-05-101 — Final dedup form, first-person→imperative post-process, suffix-naked-verb, no-fallback for intentional null

Operator: «нужно сравнить описание новой задачи с контекстом
из 10 предыдущих задач и спросить это дублирует хоть что-то
из этих задач? просто без множества усложнений. Если "да", то
не отправляем — все». Plus regressions:

  1. «Я тебе сейчас пришлю драфт письма по Артему Барсукову»
     STILL landing verbatim despite FR-CR-05-100 prompt rule.
  2. «Забежать» as a naked-verb title (not in
     FR-CR-05-99 curated list).
  3. Date `2027-04-30` from python_fallback when LLM
     correctly returned null + reasoning.

Four layered fixes:

  - **`task_dedup._SYSTEM_PROMPT` → minimal final form
    (≤700 chars).** All synonym families, worked examples,
    audience-discriminator, default-FALSE rules stripped.
    Six sentences total: «receive candidate + 10 existing,
    compare descriptions not titles, return is_duplicate
    + duplicate_of_task_id + reason». Trust the LLM
    completely.

  - **First-person → imperative Python post-process.** New
    `strip_first_person_prefix(title)` in
    `app/persistence/tasks.py` runs in `prepare_drafts`
    BEFORE `is_naked_verb_title` / `normalize_task_title`.
    Russian: «Я (тебе/вам) (сейчас/скоро/быстро) пришлю X»
    → «Прислать X» via a curated 24-entry conjugated→
    infinitive map (пришлю→прислать, отправлю→отправить,
    скину→скинуть, забегу→забежать, …). English:
    «I'll send X» → «send X». The LLM still gets the
    FR-CR-05-100 prompt rule; this is the determinstic
    safety net for when it disobeys.

  - **Suffix-based naked-verb detection.**
    `is_naked_verb_title` now also flags any single
    Russian word ≥6 chars ending in `-ться`, `-ть`, `-ти`
    as a naked verb. Catches «Забежать», «Заглянуть»,
    «Уточниться» without needing them in the curated set.

  - **No Python date fallback when LLM intentionally
    returned null.** `pipeline.py::date_node` now skips the
    `resolve_due_date` fallback when the LLM call succeeded
    with `due_date=null AND reasoning != ""`. Operator
    regression: LLM correctly judged «8 мая»/«26/02»/«30
    апреля» as context dates (not task deadlines per
    FR-CR-05-87/89), but Python fallback re-extracted them
    and emitted `2026-05-08` / `2027-04-30`. Fallback now
    only fires when the call genuinely failed (no
    reasoning emitted).

#### FR-CR-05-100 — LLM-only dedup over full descriptions; first-person → imperative title rewrite

Operator three-pack:

  1. «Взять обратную связь по PALADIN у Goldman Sachs» × 2
     and «Запланировать встречу с Atuwatse Okorodudu» × 2
     landed as duplicates with literally-identical titles.
  2. «да не нужен никакой детерминистический матч, то есть
     просто по описанию задачи надо!»
  3. «Я тебе сейчас пришлю драфт письма по Артему Барсукову»
     landed verbatim as the title — operator: «должно быть
     нормальное название и достаточно описания по контексту».

Three layered fixes:

  - **Removed the deterministic Python pre-check.** The
    `_deterministic_duplicate` helper from FR-CR-05-97/99 is
    gone; `check_duplicate` now always runs the LLM (when a
    backend is configured). Operator: trust the LLM, don't
    engineer brittle string-matching.

  - **LLM dedup sees full descriptions.** `_fmt_existing` /
    `_fmt_candidate` now feed each item with up to 1500
    chars of description (was 200 / 500). Each existing
    item lands on a multi-line block: `title / owner+due /
    desc`. Prompt rewritten to emphasize «look at the
    descriptions, not just the titles» — the operator's
    PALADIN regression had identical titles AND
    near-identical descriptions; LLM should now collapse
    them. The same-end-state rule (verb-family + specific
    subject overlap) is preserved.

  - **`title_prompt.py` — first-person commitment →
    third-person imperative.** New block teaches: «Я пришлю
    X» / «Я отправлю Y» / «I'll send Z» / «сейчас скину» →
    title is the imperative form («прислать X» / «send Z»),
    near-future adverbs («сейчас», «right now») are
    stripped, «тебе» / «you» pronouns dropped. The Артём
    Барсуков regression «Я тебе сейчас пришлю драфт письма
    по Артему Барсукову» pinned with the «прислать драфт
    письма по Артему Барсукову» imperative as the worked
    rewrite.

#### FR-CR-05-99 — Minimal dedup prompt + naked-verb rejection + title-only deterministic match

Operator: «давай промт более хороший сделаем уже раз и
навсегда, ну бред каждые синонимы добавлять, просто бери новую
задачу и 10 предыдущих задач в контексте и да/нет есть ли
дублирующие, не надо ничего усложнять» plus «таких тем тоже
быть не должно, встретиться с кем-то и тд».

Three layered fixes consolidate FR-CR-05-92/95/96/97/98:

  - **Minimal dedup prompt.** `_SYSTEM_PROMPT` rewritten from
    ~3000 chars (synonym families + worked examples) to ~1700
    chars of focused binary-classifier guidance. Single rule:
    «collapse when verb-family + specific subject overlap»;
    «default to FALSE when in doubt»; «different EXTERNAL
    audience IS a discriminator, internal-team attribution is
    NOT». No more curated synonym lists — trust the LLM.

  - **Title-only deterministic match.** `_deterministic_
    duplicate(candidate, existing)` now matches on
    `(normalized_title, owner_key)` only, dropping the
    due_date check. Operator regression: «Запланировать
    встречу с Atuwatse Okorodudu» on 2026-04-30 vs 2026-05-04
    — same work, different operator-typed dates. The Python
    fast-path now catches this before the LLM call.

  - **Naked-verb title rejection at intake.** New
    `is_naked_verb_title(title)` helper in `app/persistence/
    tasks.py` flags single-word verbs without object: Russian
    («Встретиться», «Организовать», «Подготовить», «Обсудить»,
    «Позвонить», «Написать», …) plus English («Meet»,
    «Discuss», «Schedule», «Send», «Follow up», …). Drafts
    whose title is on the list get DELETED from the session
    in `prepare_drafts` and skipped from the widget queue —
    operator: «таких тем тоже быть не должно».

#### FR-CR-05-98 — One-event collapse rule for dedup

Operator: «надо чуть строже их отбирать, чуть свободнее промт,
но не сильно». «Организовать встречу с Ryan Gariepy» (Юля) and
«Пригласить Йохана на встречу с Ryan Gariepy» (Ирина) landed
as two tasks. Different verbs (организовать vs пригласить) so
the synonym-family rule didn't apply, but BOTH revolve around
ONE upcoming external meeting.

`task_dedup.py::_SYSTEM_PROMPT` adds a single ONE-EVENT
COLLAPSE rule:

  - When BOTH candidate and existing name the SAME external
    upcoming meeting / call / event (by participant or
    topic), collapse them as duplicates EVEN IF the verbs
    are far apart («организовать» vs «пригласить» vs
    «подготовить агенду» vs «обсудить»).
  - Discriminator: «is there a single named external event
    both tasks orbit?» Yes → duplicate.
  - Escape hatch: when the second task has its OWN distinct
    deliverable that doesn't dissolve into the first
    («подготовить slide deck для встречи» — a separate
    artefact owed regardless of whether the meeting
    happens), keep separate.

The Ryan Gariepy regression pinned as the worked counter-
example.

#### FR-CR-05-97 — Deterministic title-match pre-check (no LLM) before dedup

Operator: «надо не расширять синонимы а поумнее их различать
явно». Two drafts with the LITERALLY-IDENTICAL title
«Запланировать встречу с Atuwatse Okorodudu», same owner, same
due_date landed as two separate tasks because the LLM dedup
prompt — bloated with synonym families and worked counter-
examples — was missing the obvious case.

`task_dedup.py` adds a Python pre-check that runs BEFORE the
LLM call:

  - `_normalize_title_for_match(title)` — lowercase + collapse
    internal whitespace + strip leading/trailing punctuation
    (`.!?,;:—-«»"'`) + replace `ё → е` (LLM emits both for
    the same word).
  - `_deterministic_duplicate(candidate, existing)` — return
    the matching `_ExistingItem` when ALL THREE are equal:
      1. normalised title
      2. owner key (uid or display_name, lowercased)
      3. due_date string
  - `check_duplicate` short-circuits on the first hit, returns
    `is_duplicate=True` + `reason="deterministic match: …"`,
    skipping the LLM call entirely.

Synonym/paraphrase dedup remains the LLM's domain — those rules
in `_SYSTEM_PROMPT` (FR-CR-05-92/95/96) still apply when the
deterministic gate misses (e.g. «Подтвердить» vs «Закрепить»).
The Python gate is just the «two LLM outputs that happen to be
the same string» backstop.

#### FR-CR-05-96 — Dedup synonym families: meeting-family, confirm-family, etc.

Operator: 4 separate tasks for the same Jared+Thomas meeting,
plus «Подтвердить детали партнёрства с Bosch» landed twice as
«Подтвердить» and «Закрепить» variants. FR-CR-05-95 introduced
synonym-verb dedup but only listed 3 families — too narrow.

`task_dedup.py::_SYSTEM_PROMPT` now spells out FIVE curated
synonym families:

  - **confirm-family**: подтвердить ≈ согласовать ≈ утвердить
    ≈ закрепить ≈ зафиксировать ≈ финализировать ≈
    окончательно решить ≈ confirm ≈ approve ≈ sign off ≈
    lock in ≈ finalize ≈ pin down
  - **ask-family**: узнать ≈ уточнить ≈ выяснить ≈ спросить ≈
    проверить ≈ ask ≈ check ≈ find out ≈ verify ≈ clarify
  - **intro-family**: познакомиться ≈ представить ≈ соединить
    ≈ свести ≈ интро ≈ introduce ≈ connect ≈ set up an intro
  - **send-family**: отправить ≈ выслать ≈ переслать ≈
    скинуть ≈ send ≈ forward ≈ share
  - **meeting-family**: организовать встречу ≈ пообщаться ≈
    встретиться ≈ собраться ≈ созвониться ≈ запланировать
    звонок ≈ организовать 1-1 ≈ catch up ≈ have a call ≈
    schedule a meeting ≈ set up a 1:1.

Plus a meta-rule: «обсудить X» on the same topic as a
meeting-family task is the SAME meeting (you can't discuss
without first having the meeting), and «подготовить 1-1 с X»
in a context where the meeting isn't yet scheduled means «set
it up», not «prep materials for an already-scheduled call».

Four worked counter-examples pinned (Bosch close-twice; the
two-owner Bosch case; Jared+Thomas «пообщаться» vs
«организовать 1-1»; Jared+Thomas «встретиться и обсудить» vs
«организовать встречу»).

#### FR-CR-05-95 — Title hard cap 80; third-party intent / chat outbursts; synonym-verb dedup

Operator pack:

  1. «вообще никогда названий длинных быть не может, все в
     описании! спека + тесты».
  2. «🟡 Они сами отправят ссылку» — third party will send
     it; no work owed.
  3. «🟡 Очень важный день. Надо помолиться или что ты
     делаешь в таких случаях» — chat outburst, not a task.
  4. «🟡 Подтвердить время с ADNOC» vs «🟡 Согласовать время
     с ADNOC» — duplicates (synonym verbs, same client).
  5. «🟡 Добавить в звонок с Йоханом» vs «🟡 Познакомиться с
     Йоханом» — duplicates (different verbs, same end-state
     of meeting Йохан).
  6. «🟡 Узнать о переносе звонка по Сингапуру» owner=Ирина
     vs owner=Женя — duplicates (same external call; the
     internal-team owner attribution doesn't matter).

Three layered fixes:

  - **`normalize_task_title` cap tightened: 100 → 80 chars.**
    Operator's hard rule «никогда не может быть длинных».
    The clause-break loop adds `?, ! ,` to the separator
    list so question/exclamation-marked fragments cut early.
    Word-boundary backstop also at 80 instead of 100.

  - **`detect_prompt.py` — Third-party future-intent block.**
    «Они сами отправят», «Артем сам пришлёт», «They will
    send the link themselves» = no_action. The author
    REPORTS what someone else plans to do, not delegating.
    Plus **Emotional / chat-outbursts block**: rhetorical
    sentences without a concrete deliverable («Очень важный
    день. Надо помолиться …», «Жду с нетерпением!») =
    no_action even when they look question-shaped.

  - **`task_dedup.py` — SYNONYM-VERBS + SAME SPECIFIC
    SUBJECT block.** Verb synonyms that share the same
    direct object are the same task: подтвердить ≈
    согласовать ≈ утвердить; узнать ≈ уточнить ≈ выяснить;
    познакомиться ≈ представить ≈ соединить; confirm ≈
    approve ≈ sign off; ask ≈ check ≈ verify. When the
    SUBJECT names a specific external entity / event
    (ADNOC, Singapore call, Йохан) the internal team-owner
    is NOT a discriminator — treat as duplicate when
    verb-synonyms align. The «отчёт Ирине ≠ отчёт Артёму»
    audience-discriminator from FR-CR-05-78 still holds:
    different EXTERNAL audience = different task.

#### FR-CR-05-94 — Five operator regressions in one go

Operator pack:

  1. «🟡 По Сингапуру и Гонконгу я не против, но у нас Алина —
     Chief of Investment Relations…» — 250-char opinion
     statement landed as a task title with no description.
  2. «🟡 Они у Алины в задачах есть» — status info, not work.
  3. «🟡 Узнать статус контакта … 📅 2027-02-23» — date
     hallucinated from «статус на 26/02» (status-as-of marker,
     not deadline).
  4. «📝 По переписке с 6660151534» — raw numeric Telegram uid
     leaked into the description.
  5. «при редактировании "переложи на меня" — то есть я хочу на
     себя задачу повесить, в контексте надо держать кто сейчас
     пользователь».

Five layered fixes, plus one infrastructure plumbing issue:

  - **`detect_prompt.py` — opinion / qualifier rejection.**
    New is_task=false trigger: «По X я не против, но Y» /
    «Они у Алины в задачах есть» / «Мне кажется» / «I think
    we should». Operator's Singapore / HK case pinned as the
    worked failure-mode example.

  - **`date_prompt.py` — status-as-of-date is not a deadline.**
    Pattern: «статус на DD.MM» / «status as of DD.MM». The
    date marks WHEN the status was last reported, not WHEN
    the task is due. Emit null. The «статус на 26/02 →
    `2027-02-23`» regression pinned.

  - **`telegram_ingest/service.py::prepare_drafts` runs full
    title-cap on the draft.** Previous code only did simple
    first-letter capitalisation; the FR-CR-05-72/89
    `normalize_task_title` (≤100 chars + word-boundary
    ellipsis) was only applied at `create_task_from_draft`
    time. Drafts now show capped titles in the widget,
    matching the post-Accept Task.

  - **`prepare_drafts` resolves stray Telegram uids in
    `description` to display names.** New `_resolve_uids_in_
    text(session, text)` helper finds standalone 9-15 digit
    tokens, looks them up in `team_members.telegram_user_id
    → real_name`, replaces when matched. Operator regression
    «По переписке с 6660151534» → «По переписке с Андреем»
    (or whoever 6660151534 is in the registry).

  - **`ops/wipe_tasks.py --also-wipe-sheet`.** Clears every
    row past the header in the Google Sheet via Sheets API
    `values().clear(range=A2:V)`. Operator regression: the
    DB wipe was complete but stale rows remained in the
    sheet. Header row preserved.

  - **`detect_prompt.py` — «случайно X» / «Да, X сделал»
    counter-example.** Operator's «Да, Юля случайно
    отправила» landed as a task. The «случайно» modifier
    doesn't change the past-tense completion semantics; the
    leading «Да, » confirms a question and the verb that
    follows reports what happened. Pinned in the active-
    past-tense list.

  - **Edit prompt knows the current user.**
    `_build_edit_user_prompt` / `parse_edit_with_llm` /
    `parse_draft_edit_with_llm` accept `current_user_id` +
    `current_user_label`; `apply_edit_reply_ex` resolves
    `actor` to a real_name via `_resolve_owner_link_target`
    and threads it through. New prompt block teaches: «на
    меня» / «мне» / «assign to me» / «to me» / «to myself»
    / «переложи на меня» / «assign to myself» / «передай
    мне» = `owner=<current_user_id>`.

#### FR-CR-05-93 — Detect: «уже X» / «already X» = completion recap, not a task

Operator: «"🟡 Уже написала на почту ему тоже / ну ничего) и
инвайт отправила / 📝 обсуждалось в Юля - аналитик · 2026-04-30
13:23" — ужасное описание и название».

Both source lines are completion-recap («уже написала», «уже
отправила»). The detect prompt's existing «active past tense»
list catches «отправила» / «написала» on their own, but the LLM
slipped past it because the message also contained the chat
interjection «ну ничего)» and a second clause «и инвайт
отправила» that looked like ANOTHER action.

`detect_prompt.py` strengthened:

  - **«уже X» / «already X» prefix** explicitly listed as
    a completion marker. Any verb prefixed with «уже» —
    «уже написала», «уже отправила», «уже сделал», «уже
    подтвердил», «already sent», «already called» —
    reports completion, not new work, even if the SAME
    message also says «и Y тоже» / «and Y too».
  - **Two-line operator regression** pinned as a worked
    failure-mode example:
        source line 1: «Уже написала на почту ему тоже»
        source line 2: «ну ничего) и инвайт отправила»
        → `is_task=false` (both lines are «уже X» recap;
          the «ну ничего)» is chat noise, not an
          imperative).

#### FR-CR-05-92 — Dedup: transliteration / name-variants are the same person

Operator: «"Предложить слоты для созвона с James Morgon" и
"Предложить слоты Джеймсу Моргану" — дубли».

Same person, just one mention in English transliteration and
one in Russian. Existing FR-CR-05-78 dedup gate said «different
spelling = different» and shipped both as separate tasks.

`task_dedup.py::_SYSTEM_PROMPT` gains a TRANSLITERATION block:

  - English/Latin spelling and Russian/Cyrillic spelling of
    phonetically the same person ARE the same person:
    James Morgon = Джеймс Морган; Olayan = Олаян; Ryan
    Gariepy = Райан Гариепи. Same rule for companies /
    funds / projects.
  - Diminutives / short-forms are the same person: Артём =
    Артем = Artem; Ира = Ирина = Irina; Petya = Петя = Пётр.
  - Title paraphrases that swap one name-variant for another
    but keep verb + recipient + deadline = duplicate.

The James-Morgon ↔ Джеймсу-Моргану regression is pinned as
the worked counter-example.

#### FR-CR-05-91 — Wipe CLI; admin per-person tomorrow plan; admin morning diff; self-owner badge skip; Sheets throttle

Operator three-pack:

  1. «давай обнулим все данные по задачам и начнем вести их
     заново».
  2. «выводи админу план всех людей на завтра, прям в
     разрезе людей … а перед этим изменения во вчерашнем
     плане по людям новую сделай (если есть изменения)».
  3. «"📅 Plan for tomorrow … 👤 Андрей Кузьминых" — тут
     если для меня то не пиши».

Plus the FR-CR-05-89 resync hit the Sheets `60 writes/min`
quota and dropped 21 rows on a 108-task resync.

Five layered fixes:

  - **`ops/wipe_tasks.py`** — DESTRUCTIVE bulk wipe. Deletes
    `tasks` / `action_drafts` / `task_status_history` /
    `task_subscriptions` / `google_sheets_sync` /
    `google_tasks_sync` / `daily_plan_items` + targeted
    `audit_logs` rows (digest + plan categories). KEEPS
    `team_members`, `telegram_chat_members`,
    `processed_telegram_messages`, `meeting_recordings` so
    re-ingestion picks up where it left off.
    Two-step confirm: bare `python -m ops.wipe_tasks` is a
    dry-run; `--yes` actually wipes.

  - **Self-owner badge skip in tomorrow plan.**
    `_render_tomorrow_plan_message` no longer emits the
    «👤 owner» line when `task.owner_user_id == recipient`.
    Subscribed-task lines keep the badge — they're somebody
    else's work.

  - **Admin per-person tomorrow plan.** New
    `_render_admin_tomorrow_plan_per_person` selects every
    open task across all owners for tomorrow, groups by
    owner with section headers «👤 <Name> — N tasks», sorts
    sections alphabetically with «Не назначено» pinned to
    bottom. Sent in the admin loop right after the team
    status digest. Replaces the previous
    «admin gets their own tasks» behaviour.

  - **Admin morning diff vs yesterday's plan.** New
    `_render_admin_morning_diff(session, today, admin_uid)`
    in `morning_cards.py`:
      1. Reads the most recent prior `audit_logs` row for
         the admin under `category=telegram_evening_status`,
         `action=admin`. Pulls
         `payload.per_person_plan_task_ids` (saved on the
         evening run as `{owner_uid: [task_id, …]}`).
      2. Re-runs the per-person tomorrow selector for today.
      3. Per owner, classifies each diff:
           ✅ DONE  — yesterday-only AND status=done
           🗑 DELETED — yesterday-only AND deleted_at set
           📅 DEFERRED — yesterday-only AND due_date > today
           ➖ REMOVED — yesterday-only otherwise
           ➕ ADDED  — today-only
      4. Returns rendered HTML, or `None` when no person has
         changes.
    Sent in the admin morning loop BEFORE the intro + cards
    so the operator first sees deltas, then today's owned
    cards. New `MorningCardsReport.admin_diff_sent` counter.

  - **Sheets `--write-delay-sec` throttle.** `ops/resync_
    sheet.py` defaults to 1.1s between writes (~54
    writes/min, headroom under the 60/min Sheets API
    quota). Tunable via `--write-delay-sec`. Was hitting
    429 and dropping 21/108 rows on the 108-task resync.

#### FR-CR-05-90 — Retroactive anyone-with-link writer share for legacy meeting docs

Operator: «сделай так чтобы отчеты которые генерируются в
google doc были сразу доступны для редактирования всем у
кого есть ссылка».

The behaviour was already implemented as FR-CR-05-59 — the
Fireflies pipeline calls `DocsExportService.export_summary`
without overriding `share_role`, so the default `'writer'`
kicks in and `_share_anyone_with_link` runs
`permissions().create({"type":"anyone","role":"writer"})`
on every newly-created doc. New invariant test
(`test_fireflies_pipeline_calls_export_summary_with_writer_default`)
pins the call signature so a refactor can't silently
revert all new docs to private.

For LEGACY docs created BEFORE FR-CR-05-59 was deployed
(or where the share call silently 4xx'd), new
`ops/retro_share_docs.py` CLI walks every
`meeting_recordings.google_doc_id` row and re-issues the
permission. Idempotent (the Drive API treats a duplicate
`type=anyone` permission as a no-op). Flags:

  - `--dry-run` — list doc ids without touching the API.
  - `--role {reader|writer|commenter}` — override the
    default `writer`.

Operator command:

```
sudo docker exec slack-task-bot python -m ops.retro_share_docs
```

Exit codes: 0 = all shared, 1 = at least one per-doc
failure (others still shared), 2 = bad config (missing
Drive credentials).

#### FR-CR-05-89 — Bulk-resync CLI + transcript dumps as no_action + naked-verb / proof-quote rules

Operator: «давай я все задачи дропнул в шит, перезальем
туда» plus three back-to-back regressions:

  - «🟡 Это что? / На изображении показано электронное
    письмо от Артема Соколова, отправленное Джоди и с
    копией Ирине …» (long screenshot transcript as title);
  - «🟡 Обсуждают сообщения внутри группы CEO Office с
    Ириной …» (chat-content recap as title);
  - «🟡 Встретиться» (naked verb, no complement);
  - «📝 Ryan будет в Лондоне с 4 по и предлагает …»
    (mid-sentence date-range trail-off);
  - «🟡 Подготовить письмо для MGX … 📅 2026-05-31» (date
    re-emerging despite FR-CR-05-87).

Five layered fixes shipped under one FR ID:

  - **`ops/resync_sheet.py`** — new bulk-resync CLI.
    Walks every non-deleted Task, runs the new
    `normalize_task_title` helper (FR-CR-05-72/-75/-89
    rules now in one place), clears
    `Task.google_sheets_row_id` + the matching
    `GoogleSheetsSync.row_id` so the next sync APPENDS
    fresh into A:V (FR-CR-05-86 path) instead of updating
    a stale row pointer, and pushes through
    `sheets.sync(session, task)`. Flags: `--dry-run`,
    `--include-deleted`. Operator command after «дропнул в
    шит»:
    ```
    sudo docker exec slack-task-bot python -m ops.resync_sheet
    ```

  - **`detect_prompt.py` — TRANSCRIPTION DUMP HARD RULE.**
    New paragraph in the «Return is_task=false for» list
    teaching: source paragraphs that DESCRIBE what's in a
    screenshot or chat snippet are observation, not action.
    Reject lead-in patterns: «На изображении / На скрине /
    Обсуждают / Сообщение от / В переписке / Это что?». Two
    operator regressions pinned as failure-mode worked
    examples. Exception: explicit imperative alongside the
    transcript («Это письмо от Олаяна — ОТПРАВЬ ему ответ»)
    still extracts the imperative as the task and the
    transcript as the description.

  - **`title_prompt.py` — NEVER SHIP A NAKED VERB TITLE.**
    A title that's a single bare verb («Встретиться»,
    «Подготовить», «Send», «Follow up») is useless without
    the object/addressee/topic. Always include the
    complement; if context can't fill it, append «(уточнить
    детали)» rather than ship the bare verb.

  - **`title_prompt.py` — half-emitted date ranges.** New
    LENGTH RULE addendum: if the LLM can't quote both ends
    of a date range («с 4 по 8 мая»), drop the range
    entirely («в начале мая») rather than ship the half-
    range «с 4 по и …». The Ryan-Gariepy / Лондон trail-
    off pinned as worked counter-example.

  - **`date_prompt.py` rule 11 — PROOF QUOTE OR NULL.**
    Operator policy: «либо в описание добавляй пруф либо
    сегодня». For every non-null `due_date`, the
    `reasoning` field MUST start with a verbatim quote of
    the date phrase from source. The MGX «до конца мая»
    counter-example is pinned as the canonical failure-
    mode (the «до конца мая» modifies the round, not the
    edit-letter task — emit null + reasoning explaining
    why).

`normalize_task_title` is now exported from
`app/persistence/tasks.py` (refactored out of the inline
block in `create_task_from_draft`) so the resync CLI and
any future migration can share the same logic.

#### FR-CR-05-88 — Title never ends on a preposition; description never null with context

Operator: «"🟡 Спросить слоты с / 📝 Необходимо уточнить
доступные слоты для встречи с Марко…" — почему оборван
тайтл задачи и все равно короткое описание (что за поездка
и тд)» and «"🟡 Исправлено, отправлять? / 📝 обсуждалось в
Artem/Alina/Irina · 2026-04-30 11:28" — тут тоже ничего
непонятно по контексту».

Two layered prompt fixes on `title_prompt.py::TITLE_SYSTEM_
PROMPT`:

  - **Title NEVER ends with a preposition.** Russian list:
    с, со, в, во, на, от, к, ко, по, за, у, для, из, под,
    над, о, об, про, при, через. English: with, to, for,
    of, from, by, on, in, about, at, into, onto, under,
    over, through. If the imperative ends on one, the
    complement was cut — look at source + context to
    recover it, or replace with a generic «(уточнить с
    кем / с чем)» placeholder. The «Спросить слоты с» →
    «Спросить у Марко слоты в календаре» rewrite is pinned
    as the worked counter-example.

  - **Description NEVER null when ≥1 context message
    exists.** The deterministic fallback («обсуждалось в
    <chat> · <date>») is the failure signal — operators
    can't act on it. Required behaviour: ≥2 sentences
    naming WHO said what and WHAT the work is, even when
    the source line is cryptic («Исправлено, отправлять?»).
    Names get pulled from context; the description
    surfaces the prior thread the cryptic line belongs to.
    The MGX-letter / Ирина-edits regression is pinned as
    BAD/GOOD example.

#### FR-CR-05-87 — Date must belong to the task action, not to a different entity in the sentence

Operator: «"🟠 Отредактировать письмо для MGX … упомянуть
что раунд нужно закрыть до конца мая. Это важно для
успешного завершения переговоров с MGX … 📅 2026-05-31" —
здесь раунд закрыть до 31 мая, но это не дедлайн по
задаче».

The «до конца мая» refers to the ROUND's close deadline —
a business fact going INTO the email content the user is
asking us to edit. It is NOT a deadline for the task
itself. The LLM was treating any date phrase in source as
the task's `due_date`.

`date_prompt.py::DATE_SYSTEM_PROMPT` rule 10 added: the
date must modify the TASK's verb. Discriminator: «which
verb does the date modify?»

  - «X к 5 мая» / «to do X by May 5» → date modifies X
    (the task) → use it.
  - «X — упомянуть, что Y до 5 мая» / «edit the email to
    mention that the round closes by May 31» → date
    modifies Y (the round, not the task) → emit null.

Other patterns falling under this rule: «отчёт о встрече 5
мая» (the meeting was on May 5; task is to write the
report), «напомни про вчерашний разговор» («вчера» anchors
the conversation, not the reminder), «материалы под раунд
который закрываем до конца мая», «обсудить результаты
квартала». Default when unsure: emit null and let the
downstream FR-CR-05-63 default («today 18:00») fill in.

The MGX worked counter-example with `due_date=2026-05-31`
as BAD output is pinned in the prompt.

#### FR-CR-05-86 — Sheets append/update range pinned to schema width

Operator: «у меня щас в google sheet, новые задачи
начинаются с Z листа, что делать».

`SheetsSyncService._append` and `._update` both used
`range="A:Z"` (26 cols) but `_HEADER_ROW` carries 22.
Combined with leftover content in columns W-Z (the rolled-
back legacy `dialogue` column from before FR-CR-05-15, or
operator's stray edits), Google Sheets' append heuristic
detected a wider-than-22 logical table and started placing
new rows past the schema — task data landed in column
W/X/… instead of A. Existing rows continued to render in
A-V, the new ones drifted right.

Fix: range is now `A:<col_letter(len(_HEADER_ROW))>` —
currently `A:V` for the 22-column schema. The new helper
`_col_letter(n)` handles 27+ columns (`AA`, `AB`, …) for
future-proofing. `_ensure_headers` already used this idea;
both `_append` and `_update` now share the helper.

#### FR-CR-05-85 — `ops.telegram_digest` cron registry pinned in autotests

Operator: «кроны проверь в автотестах все».

`tests/requirements/test_telegram_digest_cron.py` now pins
the contract that the operator's cron depends on:

  - `_TYPES` registry contains every supported subtype
    (`evening-status-report`, `morning-task-cards`, plus
    the legacy plan-evening / plan-morning / weekly /
    deadlines / starts-now / thread-reminders / admin-
    watchlist / morning-digest entries).
  - `evening-status-report` routes to `app.telegram_bot.
    evening_status.send_evening_status_report`.
  - `morning-task-cards` routes to `app.telegram_bot.
    morning_cards.send_morning_task_cards`.
  - `--date YYYY-MM-DD` parses to ISO and propagates as
    `today=` to the called function for both subtypes
    (so back-fill / replay commands hit the right audit
    row).
  - The LLM backend is built ONLY for evening-status-
    report; the morning flow MUST NOT touch
    `_build_llm_backend` (it renders deterministic card
    bodies, no narrative needed).

Renaming a flow function or dropping a subtype now trips a
test instead of silently breaking the next-day digest.

#### FR-CR-05-84 — Morning cards wipe yesterday's set before posting today's

Operator: «утром мне отправляй просто список моих задач
отдельными карточками, ты их как бы удаляй если они ранее
были и создавай заново с утра + просроченные туда же».

`send_morning_task_cards` now:

  - Records every (chat_id, message_id) it posts (intro DM,
    each task card, the «👀 Watching» separator) into
    `audit_logs.payload.card_messages` for today's run.
  - Before posting today's intro, looks up the most recent
    PRIOR audit row for the recipient under
    `category=telegram_morning_cards` and calls
    `sender.delete_message` on every card listed in its
    `card_messages` payload. Best-effort: per-message
    failures (Telegram refuses deletes older than 48h) are
    logged and skipped, never abort today's posting.
  - New report counter `prior_cards_deleted` exposes the
    cleanup so the cron log shows it happening.
  - Idempotency unchanged: today's audit row gates a
    re-run within the same day.

The overdue-tasks-in-the-morning behaviour (FR-CR-05-49)
already covered by `_owned_due_today`'s `due_date <
today` clause — operator's «просроченные туда же» is a
re-affirmation, not a new requirement.

`_post_one_card` return type changed bool → `int | None`
so the caller can persist the message_id. All call sites
in this module updated.

#### FR-CR-05-83 — Evening sends the tomorrow plan as a second message

Operator: «вечером ты должен отправлять статусы (в 6) —
это просто информационный дайджест и след сообщением
пост со списком задач моих на завтра с гиперссылками на
эти задачи в боте».

`send_evening_status_report` now sends a SECOND message
right after the status digest: a hyperlinked list of the
recipient's open tasks scheduled for tomorrow.

  - Selector `_owned_for_tomorrow`: same shape as the
    morning's `_owned_due_today` but anchored to
    `tomorrow=today+1`. Includes `due_date == tomorrow`,
    `due_date < tomorrow` (overdue rolls forward — the
    work is still owed), `status == in_progress`, and the
    `is_current_week` no-due-date branch.
  - Order: overdue first (🚨), then priority desc →
    due_date asc nulls-last → due_time asc → id asc.
  - Each line: priority bullet + `<a href="<bot card
    url>"><b>title</b></a>` + meta line (📅 due / 👤
    owner). The URL prefers the recipient's own
    `task.extra.telegram_cards` deep-link
    (`tg://openmessage`), falling back to `t.me/c/<chat>/
    <msg>` for supergroup-hosted cards — same logic as
    FR-CR-05-48.
  - Long lists split at task boundaries; each chunk
    stays ≤4096 chars, follow-ups carry a «(continued)»
    marker.
  - When the recipient has no tomorrow tasks, the second
    message is skipped (no empty DMs).
  - `EveningStatusReport.tomorrow_plans_sent` counter
    exposes how many recipients got the addendum.

Idempotency unchanged: today's audit row gates re-runs
of the whole evening flow.

#### FR-CR-05-82 — Description must carry every named entity; date prompt blocks implicit-Monday hallucination

Operator: «"Обсудить возможность встречи или следующей чтобы
подготовиться к раунду" — почему стоит 4 мая? категорически
не хватает описания, чтобы можно было доверить, вся фактура
в описании должна быть».

Source/context contained Fubon, Ryan Gariepy, four explicit
time-slot windows (May 5 18-21, May 6 9-12 or 17-19, May 8
9-12, May 9 17-19), and an internal draft consensus on May 6
11:30 London / 18:30 Taiwan. The bot produced:

  - `due_date=2026-05-04` — Monday after current_date=2026-04-30,
    which appears NOWHERE in the source (pure inference from a
    vague «следующей»).
  - description = 2 short sentences mirroring the title and
    naming nobody («Важно, чтобы это было согласовано с
    руководителем»).

Two layered prompt fixes:

  - **`title_prompt.py::TITLE_SYSTEM_PROMPT` — NAMED-ENTITY
    COVERAGE (HARD REQUIREMENT).** New section sits ABOVE
    «CONCRETE OVER VAGUE» and demands the LLM scan source +
    every preceding `context` message and copy EVERY person
    name, company / fund / client / project, specific date or
    time slot, amount / valuation / contract number into the
    description verbatim. ≤1 named entity in the description
    when ≥2 are in source = explicit failure. The full Fubon /
    Ryan Gariepy / four-slot regression is pinned as a worked
    BAD/GOOD example. LENGTH RULE bumped from «1-3 SHORT
    sentences, ~40-200 chars» to «3-6 sentences, ~150-600
    chars when material is available» to give the LLM room to
    pack context.

  - **`date_prompt.py::DATE_SYSTEM_PROMPT` — NO HALLUCINATING
    DATES NOT IN SOURCE.** New rule 9 forbids projecting the
    upcoming Monday unless the source LITERALLY contains
    «следующая неделя» / «next week» / «понедельник» /
    «Monday» / «к понедельнику» / «by Monday». Same constraint
    on every other weekday. Multi-candidate-dates branch:
    when source offers OPTIONS («pick one of May 5/6/8/9»),
    emit null unless one slot is unambiguously singled out
    («выбрали X», «final: Z»); never pick «the earliest».
    The exact 2026-05-04 regression is pinned as BAD output
    alongside the Fubon scenario.

Same `prompts.py::SYSTEM_PROMPT` description block updated
in parallel (it's the legacy single-call path; production now
uses the split `title_prompt.py` stage but we keep both
copies in sync for any future fall-back path).

#### FR-CR-05-79 — Owner prompt: requester ≠ doer + role-pair assistant inference

Operator: «"Артём попросил посмотреть письмо свежим взглядом"
— почему повесил на Артёма? Это его ассистент Ирина должна
делать».

Two layered fixes on `OWNER_SYSTEM_PROMPT`:

  - **Requester ≠ doer.** «попросил» / «asked» /
    «requested» / «sent us to do X» means the named person
    is the REQUESTER, not the assignee. Pinned with the
    «Артём попросил посмотреть письмо» worked
    counter-example. Default behaviour: pick the
    requester's assistant (per the routing rule below) or
    leave null.
  - **Role-pair assistant inference.** The FR-CR-05-52
    rule required the assistant's NOTES to explicitly name
    the principal («ассистент Артёма»). Operator-curated
    rows in production didn't always — Ирина's role is
    «Ассистент CEO» but notes describe her duties without
    «Артёма» by name. New rule: when principal's role is
    «CEO» AND another row's role is «Ассистент CEO» /
    «CEO Office» / «Chief of Staff», that row IS the
    assistant — no name match required. Same for «Founder»
    + «Founder's Office», «Head of X» + «X Office».

#### FR-CR-05-78 — Dedup: different recipient/deadline = different task

Operator: «ввёл текстом "подготовить отчет Ирине
послезавтра" и ничего не произошло». Listener log showed
the LLM dedup gate killed it as duplicate of «Подготовить
отчёт Артёму завтра» on the basis of shared verb +
noun.

Rewrote `_SYSTEM_PROMPT` in `app/services/task_dedup.py`:

  - Default to `is_duplicate=false` — better one extra task
    the operator merges than silently dropped real work.
  - Different recipient OR different deadline OR different
    deliverable / specific subject = different task.
  - Duplicates require ALL of subject / recipient /
    deadline to overlap.
  - Pinned the regression case as a worked example.

#### FR-CR-05-77 — Owner prompt: dative case = audience, not assignment

Operator: «"подготовить отчёт Артёму завтра" повесил на
Артёма, надо на меня». Russian dative is ambiguous —
«отчёт Артёму» can mean either «assigned to Artem» (rare)
or «report for Artem» (audience, most common). The LLM was
defaulting to the assignment reading.

`OWNER_SYSTEM_PROMPT` rule «Do NOT pick — name as
reference» rewritten: dative without an explicit doer-verb
is AUDIENCE; «for X» / «to X» in English same. Assignment
requires:

  - vocative + verb («Артём, сделай»),
  - passive-construction («сделает Артём»),
  - explicit «assign to» / «pусть» phrase,
  - or `<@…>` mention.

Otherwise null owner; downstream falls back to the
message author (who's typically the doer).

#### FR-CR-05-52 — Owner prompt routes routine work to assistant

`OWNER_SYSTEM_PROMPT` gains an ASSISTANT / DELEGATION
RULES section. NOTES are read for hints like «только
стратегические», «assistant: <Имя>», «помощник: <Имя>». A
named principal's routine work (operational reminders,
follow-ups, scheduling) gets routed to the assistant whose
NOTES name them back, while strategic / decision-making
work stays on the principal. Worked example:
«Артём, напомни Olayan про NDA» → owner = Ирина (his
assistant), not Артём.

#### FR-CR-05-51 — Listener doesn't backfill old messages on cold start

Operator: «получай данные с сейчас, в старое не ходи».
Both the Supabase TG-view poll and the Bot API getUpdates
path used to grab the freshest N messages on first poll
and create tasks for everything — replaying up to 24h of
chatter after every cold start. Both paths now seed
`_view_realtime_started_at` / `_bot_api_started_at` on
first invocation and skip messages whose `sent_at` is
strictly before that timestamp. New
`ListenerReport.skipped_pre_startup` counter.

#### FR-CR-05-50 — Task descriptions carry concrete context

`SYSTEM_PROMPT` rule #7 rewritten to demand 1-3 sentence
descriptions that include the SPECIFIC subject (which
list / which client / which doc / which numbers — copied
verbatim), names of people / projects mentioned IN the
source, and the why-this only when the source carries it.
Pinned worked example: bad title=«добавить в задачи» +
desc=«Необходимо добавить текущие задачи в список» (no
context) → corrected version naming the project / decision
context from the source.

#### FR-CR-05-49 — Overdue tasks badged with 🚨 in morning + evening

Operator: «просроченные задачи тоже выводи с эмодзи аларм
по утру и вечером в статусах также подсвечивай где дедлайн
прошёл у каких задач».

A task is "overdue" when its `due_date` is strictly before
`today` AND its status isn't `done`. Tasks without a
`due_date` are never overdue.

Evening report (`evening_status._render_task_line`):

  - Bullet logic now reads `done → ✅`, `overdue → 🚨`, else
    priority colour. The 🚨 alarm beats the priority emoji
    so it visibly stands out across all three status
    sections (Done / In progress / Todo / Subscriptions).
  - `today` threaded through `_build_groups` →
    `_render_task_line` so the overdue check uses the
    same effective date the report was generated for.

Morning task cards (`morning_cards`):

  - `_owned_due_today` selector now ALSO admits
    `due_date < today` — overdue tasks were silently
    dropped from the digest before this fix.
  - `_sort_tasks_for_morning` sort key gains an
    `overdue_rank` (0 if overdue, 1 otherwise) at the
    head of the tuple, so overdue tasks are pinned above
    even urgent same-day tasks.
  - `_post_one_card` passes `header="🚨 ПРОСРОЧЕНО · был
    дедлайн {iso}"` into `build_task_card_text` for
    overdue cards — the alarm shows above the title.
  - `_build_intro_text` adds a «🚨 Просрочено: N» line to
    the morning intro when N > 0.

#### FR-CR-05-48 — Evening report links to the bot's task card

Operator: «когда задача в статусе по вечерам — то в
гиперссылке ссылка именно на карточку с сообщением с
задачей в боте, а не с сообщением в чате».

The evening status report (`_render_task_line`) used to
hyperlink each title to `task.source_permalink` — i.e.
the original chat message that triggered the capture. The
operator wants the title to navigate to the BOT'S CARD in
their DM instead, so they can act on it (Edit / Mark done /
Subscribe) without scrolling the chat.

New helper `_task_card_url(task, recipient_chat_id,
bot_user_id)` resolves a clickable URL with this priority:

  1. The recipient's own card in
     `task.extra["telegram_cards"]` — emit
     `tg://openmessage?user_id=<bot_user_id>&message_id=<msg_id>`.
     Mobile Telegram clients honour this and jump straight
     to the message inside the recipient's DM.
  2. A supergroup-hosted card (chat_id starting with
     `-100`) — emit `https://t.me/c/<stripped>/<msg_id>`.
     Public URL form, works in any client.
  3. None — title renders plain. We deliberately stop
     falling back to `source_permalink` so chat-message
     links never re-enter the report.

`bot_user_id` is extracted from the bot token
(`<bot_user_id>:<secret>` → leading numeric chunk) inside
`send_evening_status_report` and threaded through
`_build_groups` → `_render_task_line`.

Limitation noted in code: Telegram has no public per-message
URL for DMs, so desktop / web readers may end up with a
plain title even when the recipient has an active DM card.
Mobile readers (the dominant case) get the deep link.

#### FR-CR-05-47 — Edit-reply replaces editor's card (delete + repost)

Operator wanted: «когда редактируешь карточку с задачей —
старая удаляется, только новая есть».

Pre-FR-CR-05-47 the Edit reply flow:
  - `refresh_card` edited every delivered card in place (good
    for other recipients but the editor still saw their stale
    card unmoved up the chat).
  - Plus a SECOND DM with the full re-rendered card body was
    posted under the user's reply (FR-CR-05-38). Net effect:
    two cards in the editor's chat — one stale-but-edited
    near the top of history, one fresh near the bottom.

New flow uses `replace_card_for_viewer(sender, session,
task, viewer_chat_id, reply_to_message_id)` in
`app/telegram_bot/cards.py`:

  1. Refresh OTHER recipients' cards in place
     (`update_message`) — they didn't trigger the edit but
     still need accurate state. Same UX as before for them.
  2. DELETE the editor's stale card via `delete_message`.
     Failures (message too old, Telegram error) are logged
     and swallowed — a hanging stale card is uglier than a
     crash but doesn't block task state.
  3. POST a fresh card to the editor's chat under their
     reply. Same body + keyboard as the live cards.
  4. Persist the updated `(chat_id, message_id)` list onto
     `task.extra["telegram_cards"]` and update
     `task.card_channel/card_ts` to the new pair so legacy
     single-card readers stay consistent.

The FR-CR-05-38 «full re-rendered card under reply» is now
the same message that replaces the deleted card — no more
double-card layout.

#### FR-CR-05-46 — Multi-task extraction in prompt + tool schema

Operator: «надиктовал "мне нужно разработать бота а ещё мне
нужно сделать дашборд" — это две задачи, он как одну взял».

`IntentClassification.tasks: list[TaskDraft]` had been wired
in code since FR-CR-05-05, but the LLM tool schema only
exposed a singular `task` field — so the LLM physically
couldn't emit a `tasks` array. The model crammed two verbs
into a single title.

Fix is two-part:

  - `INTENT_TOOL_PARAMETERS` (`app/intent/llm_backends.py`)
    now carries a `tasks` array — each item the same shape
    as the legacy `task`. The LLM is told this is the
    canonical field; legacy `task` stays for back-compat
    and is normalised to `tasks=[task]` by the
    `IntentClassification` validator.
  - `SYSTEM_PROMPT` rule #3 rewritten with explicit
    splitting signals (conjunctions «а ещё», enumerations,
    two-verb sentences, two distinct objects) and a
    worked example pinned in the prompt
    («разработать бота» + «сделать дашборд» → two tasks).

Tests pin both the prompt content (split signals + worked
example) and the tool-schema shape (`tasks` array with the
full TaskDraft fields).

#### FR-CR-05-45 — `/start` welcome widget

`/start` and `/help` in any private DM with the bot now
return a single onboarding message:

```
👋 Привет! Я веду список задач.
📝 Напиши задачу текстом или продиктуй голосом — я разберу.
Можно списком: «первая задача …, вторая задача …» — раскидаю
в виде отдельных карточек.
🚦 На каждой карточке кнопки: Start, Edit, Mark done, Subscribe.
📊 Каждый вечер пришлю краткий статус по всем задачам.
☀ Каждое утро — карточки на сегодня.
```

The `tick()` loop intercepts the slash-command BEFORE the
ingest pipeline so the classifier isn't called and no Task
row lands. Group chats fall through to normal capture
(groups don't need onboarding).

Multi-task voice/text dictation, raised by the same
operator instruction, is delivered by the existing
FR-CR-05-05 `process_all` flow — no new code needed there
once FR-CR-05-44 made voice top-level captures work and
FR-CR-05-46 unlocked multi-task extraction.

#### FR-CR-05-44 — Top-level voice / audio capture in DM

A voice message in a private DM that's NOT a reply to a
prompt used to fall through with `msg.text == ""` and hit
the ingest pipeline as silent no_action — operator
complaint: «я отправил аудио в бота и все сломалось».

The `tick()` loop now transcribes via Whisper (same helper
used by FR-CR-05-14 in Edit/Done replies) right after the
pending-reply check and rebuilds the message dataclass with
`text=transcript`. If transcription returns empty in a
private DM, the user gets an explicit nudge: «🎙 Не разобрал
голос. Попробуй ещё раз или напиши текстом.». Group chats
fall through silently.

#### FR-CR-05-43 — Edit-fanout DM silenced

The cross-channel subscriber dispatch (FR-CR-05-02) used
to DM every non-owner subscriber a one-line «✏ #N title —
description=…, priority=high by 222968032» on every edit.
Format was technical (`field=value`) and showed raw uid as
actor. Operator: «такие сообщения после редактирования
писать не надо».

Removed the `dispatch_edit` call from
`apply_edit_reply_ex`. The helper itself stays in
`subscriber_updates` for any future reuse, but no caller
fires it now. Status-change fanout
(`dispatch_status_change`, «started» / «done» events) is
unaffected — high-signal transitions still fan out.

#### FR-CR-05-42 — `team_members.notes` column → TEXT

Production logs showed every Sheet → DB pull failing with
`StringDataRightTruncation` because the operator pasted
multi-paragraph notes (>512 chars) into the Team sheet.
Migration `0019_team_members_notes_text` issues
`ALTER COLUMN ... TYPE TEXT` on PostgreSQL; SQLite
(tests) is a no-op since VARCHAR maps to TEXT internally.
The 200-char cap on the owner-prompt block (FR-CR-05-31)
keeps prompts reasonable even with very long DB values.

#### FR-CR-05-41 — Morning task cards (one interactive card per task)

The legacy `plan-morning` posted a single bullet-list DM
that didn't expose the per-task action buttons. Operator
wanted: «утром карточки с задачами все что на день надо
сделать друг за другом по порядку — но красиво с эмодзи,
минималистично, с гиперссылками».

New `app/telegram_bot/morning_cards.py` →
`send_morning_task_cards`:

  - One intro DM «☀ Доброе утро — задачи на {date}: N».
  - Then one full interactive task card per task, identical
    to the live cards (`build_task_card_text` body +
    `task_card_keyboard` permissions). Title hyperlinks to
    the source message, owner deeplinks via FR-CR-05-19/26.
  - Selector: owned tasks with `due_date==today` OR
    `status==in_progress` OR (`is_current_week` AND status
    in todo/backlog AND no firm `due_date`). Subscribed
    tasks (due today / in flight, owned by someone else)
    follow under a thin «— — —\n👀 Подписки» separator —
    but only when the user actually has owned cards above.
  - Order: priority desc → due_time asc → start_time asc →
    id asc.

Idempotent per (user, date) via
`audit_logs.category=telegram_morning_cards`. Wired into
`ops/telegram_digest.py` as `--type morning-task-cards`;
the legacy `plan-morning` / `morning-digest` subtypes stay
in place so the operator can swap their cron entry once
they're satisfied.

#### FR-CR-05-40 — Evening status report (per-task LLM narrative)

Operator wanted: «вечером отправляй список всех задач что
сейчас в todo/inprogress и done — статус всех задач
актуальный, для админа и для каждого человека и на которые
он подписан, может быть в несколько сообщений…
информационный, потом утром карточки». This is the
informational digest; FR-CR-05-41 covers the morning
cards.

New `app/telegram_bot/evening_status.py` →
`send_evening_status_report`. For every Telegram user
(owner ∪ subscriber):

  - ✅ Сделано сегодня — history-driven (any
    `task_status_history` transition to `done` whose `at`
    falls within today UTC).
  - 🚀 В процессе — current `in_progress` ownership.
  - 📋 Todo — open backlog/todo, weighted to current week,
    sorted due_date asc nulls-last.
  - 👀 Подписки — open tasks the user follows but doesn't
    own.

For each task `compose_status_narrative` calls
`OpenAIBackend.complete_text` with a 1-line RU narrative
prompt fed the title + description + recent
`task_status_history` (last 3 days, max 6 transitions). One
LLM call per task (operator's instruction: «по одному той
же логикой обрабатывать» — different threads of discussion
stay separated). Fails open: an LLM error or empty response
falls back to a deterministic «срок X, приоритет Y, статус
Z» line so the digest still ships when OpenAI is down.

Each line renders as Telegram HTML — title wrapped in `<a
href=permalink>`, owner in `_owner_html_link` (numeric
deeplink → `t.me/<handle>` → plain text). Long reports
auto-split at line boundaries: target 3800 chars to keep
under the Telegram 4096-char hard cap with 10% headroom.
Continuation messages start with `(продолжение)` so the
operator knows it's the same report.

Admin uids additionally get a consolidated «Сводка по
команде» with EVERY active user's tasks regardless of
ownership; emitted under a separate `action=admin` audit key
so the per-user and admin DMs don't shadow each other.

Idempotent per (user, date) via
`audit_logs.category=telegram_evening_status`. Wired into
`ops/telegram_digest.py` as `--type evening-status-report`;
the LLM backend is built via `ops.telegram_ingest._build_
llm_backend` and passed through (None when no key is set →
deterministic fallback).

Real-time per-task status DMs (status change → instant DM
to subscribers) are out of scope here — same
`compose_status_narrative` helper will drop straight into
that flow when added.

#### FR-CR-05-39 — Fireflies meeting-recording pipeline (3rd source)

Operator request: «мне надо подключить ещё один источник
данных, помимо слака и телеграма — Fireflies». For each new
recording:

  1. Download the mp3 (Fireflies hosts it).
  2. Whisper transcribe (`whisper-1`).
  3. Detailed RU summary via gpt-4o (≥3000 chars,
     structured: МЕТА / КЛЮЧЕВЫЕ РЕШЕНИЯ / ОБСУЖДЕНИЕ /
     СЛЕДУЮЩИЕ ШАГИ / ОТКРЫТЫЕ ВОПРОСЫ).
  4. Export the detailed summary to a Google Doc named
     after the meeting (parent folder
     `FIREFLIES_DOCS_FOLDER_ID`).
  5. Short summary ≤2000 chars (TG-friendly), DM'd to every
     `TELEGRAM_ADMIN_USER_IDS` recipient.
  6. Task extraction via the same `record_intent`-style
     OpenAI tool call, with the team registry rendered into
     the prompt as `known_employees` (name / role / notes —
     same shape FR-CR-05-31 uses for owner prompts). All
     extracted tasks land with `due_date=date.today()`,
     `source_kind=fireflies`, `is_current_week=True`. Owner
     resolves through the FR-CR-05-09 admin-fallback chain.

Each step writes its artefact onto a `meeting_recordings`
row + flips a progress flag (`audio_downloaded` →
`transcribed` → `detailed_summarised` → `doc_exported` →
`short_summary_sent` → `tasks_extracted`). Re-running on a
finished recording short-circuits with
`skipped_reason='already_processed'`. Per-step failures
record `last_error` and abort the rest of the pipeline so
the next run picks up where it crashed.

Two ingest modes:

  - **One-shot**: `python -m ops.migrate_fireflies --newest --limit 5`.
    Pulls the last N transcripts and runs each through
    `FirefliesPipeline.process_one`. Used for the initial
    backfill / smoke test.
  - **Real-time**: the Telegram listener also polls the
    Fireflies API every `FIREFLIES_POLL_INTERVAL_SECONDS`
    (default 30) when `FIREFLIES_REALTIME_ENABLED=true`.
    Reuses the same `process_one` code path. Off by
    default; the operator flips the flag to opt in.

Fully gated behind `FIREFLIES_API_TOKEN`: empty token =
disabled, no DB rows touched, `migrate_fireflies` exits 2,
listener silently skips the poll. Migration `0018` adds
`'fireflies'` to the `task_source_kind` enum (PostgreSQL
`ALTER TYPE … ADD VALUE IF NOT EXISTS`) + creates the
`meeting_recordings` table with the progress flags above
and a unique index on `fireflies_id`.

#### FR-CR-05-37 — Mark-Done click transitions immediately

The Mark Done button used to open a force-reply «artifact?»
conversation that required either a link / note OR `/skip`.
Operator complaint: «зачем нажимать ещё `/skip`, если я
ничего не хочу добавлять».

New flow:

  - Click Mark Done → task transitions to `done` IMMEDIATELY
    (`handle_done` runs in `_open_done_conversation` instead
    of being deferred to the reply step). Card refreshes in
    place.
  - Bot posts an optional follow-up: «✅ Task #N marked as
    done. Хочешь — ответь сюда ссылкой или коротким
    комментом, добавлю в карточку. Иначе просто пропусти.»
    No `force_reply`; the user can ignore.
  - When the user does reply, `apply_done_artifact_reply`
    stores the text/URL on `completion_artifact` /
    `completion_artifact_kind`. No more `/skip` carve-out and
    no transition attempt (already done).

#### FR-CR-05-38 — Edit reply: post full updated card, not a receipt

The FR-CR-05-32 receipt («✓ Готово / 👤 owner → 222968032»)
was using raw uids and didn't show the operator the new state
of the task. Operator wanted to see the WHOLE updated card
right under their reply.

After a successful edit reply the listener now:

  - Edits the original card in place (`refresh_card` /
    `refresh_draft_widgets`, unchanged).
  - Sends a fresh DM with the FULL re-rendered card body
    (`build_task_card_text` / `_build_draft_widget_text` —
    same code paths the live card uses, with all the
    FR-CR-05-26 / 05-19 / 05-20 owner / link rendering).
    Reply-to the user's message so it appears in context.
  - Falls back to a clarification nudge when the user's reply
    asked for a vague owner change («другого оунера») that the
    LLM didn't resolve.

The small text-receipt helper `format_edit_receipt` stays in
the codebase for fallback use but is no longer wired into the
default Edit flow.

#### FR-CR-05-36 — Pull every new message per poll (large default batch)

The FR-CR-05-35 listener-side view poll initially capped at 50
rows per tick, which would skip messages on a busy deploy.
Operator wanted «every new message every 30 sec».

Default batch bumped to 500 (`VIEW_POLL_BATCH_SIZE=500`). One
SQL roundtrip per poll covers any realistic burst; the
FR-CR-04-26 per-message bookmark short-circuits already-
processed rows, so the actual work is bounded by «what's new
since last poll», not by `batch_size`.

If a deploy ever sees more than 500 new messages in 30 sec, the
operator bumps `VIEW_POLL_BATCH_SIZE` further or runs
`ops.migrate_telegram_history --newest --limit N` to catch up
manually.

#### FR-CR-05-35 — Real-time poll of the Supabase TG view

The cron-driven `ops.telegram_ingest` pulls messages from the
read-only Supabase view, but it required an external scheduler.
The default deploy had no cron — operators were stuck running
the migrator manually. New behaviour: the live listener also
polls the view every ``VIEW_POLL_INTERVAL_SECONDS`` (default
30) and runs each fresh message through `prepare_drafts` +
`post_draft_confirmation`, same path the migrator uses.

Already-processed messages short-circuit on the per-message
bookmark (FR-CR-04-26), so a 50-row re-pull every 30 s is
essentially free on a quiet day.

Toggleable via env:

  - ``VIEW_REALTIME_ENABLED=true`` — turn on listener-side
    polling. Default off so existing deploys keep their cron-
    driven flow without surprise.
  - ``VIEW_POLL_INTERVAL_SECONDS`` (default 30).
  - ``VIEW_POLL_BATCH_SIZE`` (default 50) — how many newest
    rows to read per poll.

When disabled, no source-view connection is opened from the
listener. When enabled but the reader is unconfigured (no
``TELEGRAM_SOURCE_DATABASE_URL``), the poll silently no-ops.

Errors during a poll are logged + swallowed — Telegram
getUpdates traffic keeps flowing regardless.

#### FR-CR-05-34 — Confirm-widget button order (Reject / Edit / Accept)

Per operator feedback the buttons on the confirm widget were
re-ordered from `[Accept, Edit, Reject]` to
`[Reject, Edit, Accept]`. Accept being the rightmost / last-tap
button is the «commit after review» action; Reject being the
leftmost is the safe «I'm out» choice. Easier to avoid an
accidental Accept on a not-yet-read draft.

`_looks_like_confirm_widget` (the heuristic that routes Edit
clicks to the draft-edit flow vs. the task-edit flow) now
matches order-agnostic — it accepts both the new
`[ignore, edit, confirm]` and the legacy
`[confirm, edit, ignore]` so widgets in flight from before the
upgrade still route correctly.

#### FR-CR-05-33 — Tombstone / reject lines render actor name, not uid

The Delete and Reject paths produced lines like
«🗑 Task #155 — написать Крису — deleted by 222968032».
Operator complained that the bare numeric uid was confusing —
they wanted to see the human name like everywhere else.

`_resolve_actor_label(session, actor_uid)` reuses the
FR-CR-05-26 / 05-20 owner-link resolver to find the actor's
`real_name` (or `@handle` fallback) from `team_members` /
`telegram_chat_members`. `render_tombstone` and
`render_draft_rejected` accept an optional `session` argument
and use it to render «deleted by Андрей Кузьминых» when a
matching row exists. Without a session (back-compat), the raw
uid stays — same behaviour callers always had.

The listener's callback dispatch threads `session` through to
both render helpers, so DM-rendered tombstones now show the
team name automatically.

#### FR-CR-05-32 — Edit-on-task receipt + ambiguous-owner rule

Two operator-side fixes after live testing the Edit reply
flow on a task card:

**Visible receipt after the edit lands.** Previously the bot:
silently updated the original card (often far up in the chat
history); deleted the «✏ Edit task #N» prompt; left the user's
own reply in place with no visible feedback. Operator
complained that «what I wrote disappears, the card looks
new» — they had to scroll up to find the edited card. New:
after a successful `apply_edit_reply_ex` the bot replies to
the operator's edit message with a short receipt:

```
✓ Готово
📅 due → 2026-04-30
👤 owner → Андрей Кузьминых
```

When the operator's reply also asked to change the owner but
the LLM didn't resolve a target, the receipt appends a single
hint line: «🤔 ответственного хотел поменять? уточни на кого
именно». Same path covers Edit-on-draft.

**Ambiguous-owner rule in the LLM prompt.** «другого оунера»,
«не Алину», «another owner» without a specific name no longer
clears or guesses. The Edit prompt explicitly tells the LLM
to OMIT the `owner` field on vague phrases — better to leave
the existing owner unchanged and let the receipt nudge the
operator to clarify.

#### FR-CR-05-31 — Owner prompt: role + notes are the source of truth

The operator hand-curates `team_members.role` /
`team_members.notes` on the Sheet to describe what each teammate
is responsible for. The owner-extraction prompt previously
treated those columns as «just disambiguation hints» — the LLM
used them only when several rows shared a first name. For
unnamed assignments («нужно ответить инвестору Olayan»), the
LLM had no signal and returned ``null``.

Two fixes:

**Stronger system prompt.** New SOURCE-OF-TRUTH block tells
the LLM: when the source describes work without naming a
person, pick the teammate whose role / notes match the
responsibility area. Three concrete examples (investor
relations, NDA templates, EMEA sales) anchor the model's
behaviour.

**Wider notes column.** The user-prompt table truncated `notes`
to 60 chars, clipping operator-written blurbs before the LLM
could see them. New cap is 200 — enough for «ответственная за
инвестор-релейшнс, готовит cap-table и ходит на встречи с
инвесторами», bounded so the prompt stays compact.

#### FR-CR-05-30 — Pull skips timestamp-only diffs (no-op poll is silent)

Live observation: the FR-CR-05-28 listener-side polling printed
`updated=53` every minute even when the operator hadn't touched
the Sheet, because `upsert_from_sheet_rows` always set
`last_synced_at=now` and incremented the updated counter.

Fix: compare each field BEFORE assigning. Only set fields that
actually moved; bump `last_synced_at` and the `updated` counter
only when at least one real data field changed. Timestamp-only
diffs are now invisible — the listener log fires only when the
operator actually edited something on the Sheet.

#### FR-CR-05-29 — Sheet pull merges duplicate rows on UNIQUE conflict

The auto-seed often produces TWO `team_members` rows for the
same teammate: one from `chat_members` (numeric TG id only) and
one from Slack `employees` (Slack uid only). When the operator
consolidates them on the Sheet by editing one row to carry
BOTH ids, the previous `--pull` crashed on
``UniqueViolation`` because the OTHER row still owned the
`slack_user_id` (or `telegram_user_id`) being moved over.

`upsert_from_sheet_rows` now detects the conflict before
applying:

  - When `new_values["telegram_user_id"]` would collide with a
    DIFFERENT row's `telegram_user_id`, that other row is
    deleted.
  - Same for `slack_user_id`.

The operator's intent is clear (they're merging duplicates), so
auto-deleting the orphan is the right call. After the merge the
canonical row carries both identities and the orphan is gone.

#### FR-CR-05-28 — Listener-driven periodic Sheet → DB poll

FR-CR-05-11 documented bidirectional sync via cron, but the
default deploy has no cron set up — operators were stuck running
`--pull` manually after every Sheet edit. New behaviour: the
TelegramListener itself polls both Sheets every
``SHEET_POLL_INTERVAL_SECONDS`` (default 60) and applies edits
to the DB.

Each tick of the listener checks the elapsed-since-last-pull
clock; when the interval has passed it runs:

  - `TeamSheetSync.pull(session)` — operator's `team_members`
    edits land in the DB.
  - `SheetsPullService.pull(session)` — operator's task edits
    (status, owner, due-date, etc.) land in the DB; status
    changes route through `TransitionService` per FR-CR-05-11.

Each pull runs in its own `session_scope` so a transient Sheets
HTTP error doesn't poison the listener's main transaction;
errors log + swallow, the next tick retries. Setting
``SHEET_POLL_INTERVAL_SECONDS=0`` disables the in-listener poll
(useful when running an external cron instead).

Operator workflow now:

  1. Edit a cell in the Tasks or Team Sheet.
  2. Within ~60 s the listener picks up the edit and updates
     the DB.
  3. The next render of the affected card / widget reflects the
     new state.

No `--pull` invocations needed for the operator's normal
workflow.

#### FR-CR-05-27 — Auto-add new chat users + non-destructive `--push`

Two operator-friendly registry tweaks after losing a round of
manual Sheet edits to an over-eager `--push`.

**1. Listener auto-creates `team_members` rows for new users.**
FR-CR-05-21's `_enrich_team_member_row` previously bailed out
when the matching team_members row didn't exist; new teammates
appearing in chats stayed invisible until the operator
manually added them. Now: when a user observed by the listener
has no team-row yet, INSERT one with whatever fields the
observation provides. Likely-bot rows (`bot` / `_bot` /
`office1` / `notif` heuristic) start `active=False` so they
don't pollute the LLM's owner-candidate list.

**2. `TeamSheetSync.push` is non-destructive.** The previous
`clear + rewrite` push lost any operator edit that hadn't been
`--pull`-ed beforehand. The new `push`:

  - Reads the current sheet contents.
  - Appends only DB rows that aren't on the sheet yet (matched
    by `id`, `telegram_user_id`, or `slack_user_id`).
  - Never touches existing rows — operator edits are safe.

Trade-off: deletions in the DB no longer propagate to the sheet
on push. The sheet is the operator's source of truth; deletions
flow Sheet → DB via `--pull` instead.

First-time bootstrap (sheet completely empty) still writes the
full DB table so the operator has a starting point.

Recommended workflow now:

  1. Operator edits the Sheet (`real_name`, `role`, `email`, …).
  2. `python -m ops.sync_team --pull` brings edits into the DB.
  3. New users appearing in chats land in DB automatically (via
     the listener).
  4. `python -m ops.sync_team --push` appends those new users
     to the Sheet without touching existing operator edits.
  5. Operator polishes the new rows on the Sheet, GOTO step 1.

#### FR-CR-05-26 — Owner display: real_name first, link only on `@username`

Operator-driven simplification of the owner-rendering rules
(replaces the multi-tiered chain from FR-CR-05-19/20):

**Display priority** (visible label):

  1. ``team_members.real_name`` (or `chat_members` fallback) —
     same teammate renders identically across every card.
  2. ``task.owner_display_name`` (with leading `@` /
     `<@Uxxx>` stripped — the visible label is always the
     plain name, never the handle).
  3. ``task.owner_user_id`` raw — last resort (numeric TG id
     or Slack uid as plain text).

**Link priority** (hyperlink wrapping the label):

  1. ``team_members.telegram_username`` (or `chat_members`
     fallback) → `https://t.me/<handle>`.
  2. ``@handle`` parsed off the original `task.owner_display_name`
     when the registry has nothing → `https://t.me/<handle>`.
  3. **Otherwise → plain text.** No `tg://user?id=` fallback any
     more — that link form often rendered silently in cross-chat
     DMs and looked broken to operators («ссылку не выводи если
     username нет»).

`_resolve_owner_link_target` returns a 3-tuple now:
``(telegram_user_id, telegram_username, real_name)``. New
`_resolve_owner_display(task, real_name=...)` picks the display
label per the rules above. New `_handle_from_display` extracts
the bare handle from a `@…` display so an `@`-only display still
hyperlinks even without a registry hit.

After this change, the owner cell on a card reads as one of:

  - `<a href="https://t.me/<handle>">Real Name</a>` (best case)
  - `<a href="https://t.me/<handle>">handle</a>` (no real_name)
  - `Real Name` (no handle, plain text)
  - `<numeric TG id>` (nothing else available, plain text)

#### FR-CR-05-25 — Read `sender_username` + `message_link` from the source view

The colleague's Supabase view turned out to ship two columns
that we previously didn't use:

  - **`sender_username`** — the sender's @-handle, separate
    from the display name. We were heuristically guessing whether
    `sender_name` was a username or a real name; the dedicated
    column gives us both cleanly.
  - **`message_link`** — pre-computed `t.me/c/<chat>/<msg>` URL
    that Telegram itself produced. Correct for every chat shape
    (incl. private), no chat-id form-guessing required on our
    side.

`_FIELD_MAP` extended with two new fields (`username`,
`permalink`) covering common variants. `TelegramSourceMessage`
gained both fields. `_map_row` extracts and normalises them
(strips a leading `@` from username; trims permalink). The
reader's `distinct_users()` now returns
`{user_id, user_name, username}` triples.

`seed_from_telegram_source` uses the dedicated `username` when
available; falls back to the legacy heuristic on views without
the column. ALSO BACKFILLS existing rows: re-running
`--seed` after the view gains `sender_username` populates blank
`telegram_username` / `real_name` on rows that were inserted
before. Operator-edited values stay untouched.

`_telegram_permalink` prefers `message.permalink` from the view
when set; falls back to the chat-id reconstruction otherwise.
The 🔗 link on the widget now appears for ANY message the view
has a URL for — including private chats where the bot-side
reconstruction returns `None`.

After re-running `python -m ops.sync_team --seed --pull --push`
on a deploy that has the new view columns, every team_member
that ever sent a message gets their @-handle filled in
automatically — no more `--enrich-bot-api` needed for them.

#### FR-CR-05-24 — Bot-API enrichment for never-observed users

`telegram_chat_members` only carries usernames the live listener
has actually OBSERVED — users who have never sent a message in
a chat the bot is in stay invisible there even after a
`--backfill` pass. The Telegram Bot API's `getChat(<user_id>)`
returns the user's public profile (`username`, `first_name`,
`last_name`) for any user the bot has ever interacted with —
they /started the bot, replied to a bot message, or are a
member of a chat the bot is in.

New CLI flag `python -m ops.sync_team --enrich-bot-api` walks
every sparse `team_members` row (numeric id + blank
`telegram_username` / `real_name`), calls `getChat`, and adopts
the returned profile fields. Slower than `--backfill` (one HTTP
call per row), but reaches a wider set of users. Operator-edited
values are NEVER overwritten.

Recommended one-line catch-up after upgrading:

```
python -m ops.sync_team --backfill --enrich-bot-api --pull --push
```

`--backfill` runs first (cheap, local DB), `--enrich-bot-api`
catches the rest, then `--pull` / `--push` round-trips the Sheet.

#### FR-CR-05-23 — One-shot team_members backfill from chat_members

FR-CR-05-21 auto-enrichment runs on every NEW listener
observation, but rows seeded BEFORE that fix landed (the bulk of
the registry on a deploy that came up before the auto-enrich)
stayed sparse — `telegram_username` / `real_name` blank — even
though `chat_members` already had the matching usernames /
names from prior traffic.

New CLI flag `python -m ops.sync_team --backfill` walks every
`team_members` row, looks up the most recent
`telegram_chat_members` observation for that `user_id`, and
fills in BLANK fields. Operator-edited values are preserved.

Combine with the existing flags for a one-line catch-up after
upgrading:

```
python -m ops.sync_team --backfill --pull --push
```

After this pass the owner-deeplink resolver (FR-CR-05-19/20)
finds an `@handle` for every user the listener has ever seen,
and widget owner labels start hyperlinking without further
manual Sheet edits.

#### FR-CR-05-22 — Title prompt: no placeholder pronouns, no 1st-person-plural

Two more description-quality bugs from the live test:

- «найти выходы на **указанных людей**» — vague placeholder
  pronoun where the context already named the actual targets.
- «**Будем рады**, если сможешь соединить» — first-person-plural
  copy-paste from the source message; descriptions are about a
  task assigned to ONE specific owner, «we» / «нам» / «будем»
  have no place there.

Title prompt extended with two pinned blocks:

**CONCRETE OVER VAGUE.** Forbids placeholder phrases like
«указанных людей», «правильной командой», «нужного человека»,
«as discussed», «the right people» when the context block names
the real entities. When context truly doesn't name them, write
«(кого именно — уточнить)» / «(детали — уточнить)» — the
operator should never have to guess what «указанных» refers to.

**THIRD PERSON.** Forbids 1st-person plural — «нам надо», «будем
рады», «we need to», «we'd love to». When the source uses «we»
/ «нам», the description rewrites in third person naming the
actual party (the chat / team / specific person from context).

#### FR-CR-05-21 — Registry-canonical display, listener auto-enriches team_members

Two more iterations after the live test:

**1. Registry display always wins over LLM-extracted display.**
Same teammate landed as «Артем» on one card and «Артем
Соколов» on another, depending on what fragment of the source
message the LLM clipped onto `owner_display_name`. The
`_resolve_owner` step 1 used to backfill the registry's display
only when LLM left it blank. New rule: when the resolved
`owner_user_id` matches a `team_members` row, the registry's
`display_name` / `real_name` ALWAYS overrides whatever the LLM
extracted. The Sheet is the operator's source of truth.

**2. Listener auto-enriches `team_members` from observations.**
When the live listener sees a message from a user who has a
`team_members` row but whose `telegram_username` / `real_name`
fields are blank (auto-seed wrote sparse rows for users only
known by `telegram_user_id`), the observation populates the
missing fields. Operator-edited values are NEVER overwritten —
only blanks get filled in. After a few minutes of normal
traffic the registry self-completes for every user the bot has
seen, and owner deeplinks start working without manual Sheet
edits.

#### FR-CR-05-20 — Owner deeplink prefers public `t.me/<handle>`, with chat-members fallback

50-msg run after FR-CR-05-19 produced HTML with
`<a href="tg://user?id=402006206">Юля - аналитик</a>`, but the
Telegram client rendered it as plain text. The Bot API only
makes `tg://user?id=<uid>` clickable as a mention when the
tagged user is a member of the chat where the message is shown
— and the bot's DM with the operator obviously doesn't include
the owner. The public `https://t.me/<handle>` form has no such
restriction.

Two-part fix:

**1. Reorder `_owner_html_link` priority.** `t.me/<handle>` now
wins over `tg://user?id=<uid>`. Final chain:

  1. Registry-resolved `tg_handle` → `https://t.me/<handle>`
  2. `display` matches `@<handle>` → `https://t.me/<handle>`
  3. Registry-resolved `tg_user_id` → `tg://user?id=<uid>` (last-ditch)
  4. Numeric `owner_user_id` → `tg://user?id=<uid>`
  5. Otherwise → plain text

**2. `_resolve_owner_link_target` falls back to `telegram_chat_members`.**
Auto-seed populates `team_members` from the Supabase view,
which often has the `from.username` field empty (the colleague's
pipeline doesn't always preserve it). The live listener,
however, writes `telegram_chat_members.username` on every
observed message — that table has the operator's `@andre_andreevich`
even when team_members has only the numeric id. New rule: when
the team_members row has `telegram_user_id` but no
`telegram_username`, look up the most recent
`telegram_chat_members` row for that `user_id` and adopt its
username.

After both changes, every owner whose @-handle is anywhere in
either table renders as a clickable `t.me/<handle>` link in any
Telegram client, regardless of chat membership.

#### FR-CR-05-19 — Owner deeplink via team-registry lookup

The FR-CR-05-18 owner-link helper hyperlinked numeric TG ids and
display strings that already had the `@handle` form. Real
displays like «Юля - аналитик» / «Алина Колпакова» (plain Russian
real-names) rendered as plain text — operators couldn't tap to
DM them.

New `_resolve_owner_link_target(session, owner_user_id,
owner_display_name)` looks up the `team_members` row matching
either `owner_user_id` (numeric → `telegram_user_id`; otherwise
→ `slack_user_id`) or, when that misses, the display name
matched against `telegram_username` / `real_name`
(case-insensitive). Returns the registry row's
``(telegram_user_id, telegram_username)`` tuple — either or both
may be ``None`` for a half-populated row.

`_owner_html_link` now takes optional ``tg_user_id`` /
``tg_handle`` kwargs and prefers them when the local ones don't
hyperlink. Resolution chain:

  1. Registry-resolved numeric TG id → `tg://user?id=<uid>`
  2. Numeric `owner_user_id` → `tg://user?id=<uid>`
  3. Registry-resolved `tg_handle` → `https://t.me/<handle>`
  4. `display` matches `@<handle>` form → `https://t.me/<handle>`
  5. Otherwise → plain text

`build_task_card_text` and `_build_draft_widget_text` now accept
an optional ``session`` and feed it to the resolver. Every call
site in `app/telegram_bot/cards.py` already had a session
available; threaded through. Renderers without a session (test
paths, future callers) keep the FR-CR-05-18 behaviour
unchanged — no regression.

#### FR-CR-05-18 — Title-as-link, owner @handle deeplink fallback

Visual cleanup follow-up to FR-CR-05-17.

**1. Title is the source-message hyperlink.** The standalone
`🔗 t.me/c/<chat>/<msg>` line was visual noise — operators
weren't tapping the URL, they were looking at the title. New
layout wraps the bold title in the deeplink:

```
🟡 <a href="https://t.me/c/.../..."><b>title</b></a>
📝 description
👤 <owner-deeplink> · 📅 due
```

A tap anywhere on the title jumps to the original chat message.
When the source has no shareable URL (private DM, basic group),
the title falls back to plain `<b>title</b>` without a broken
`<a href="">` wrapper.

**2. Owner `@handle` deeplink fallback.** The original
FR-CR-05-16 helper only hyperlinked when `owner_user_id` was a
numeric Telegram user_id. Slack-only teammates whose
`owner_display_name` carried `@username` rendered as plain
text. New rule:

  - Numeric `owner_user_id` → `tg://user?id=<uid>` (preferred,
    opens private chat inside Telegram).
  - `display` matches `@<handle>` (5–32 ASCII alnum +
    underscore, must start with a letter) →
    `https://t.me/<handle>`.
  - Otherwise → plain text.

Same helper is used in the live task card and the confirm
widget.

#### FR-CR-05-17 — Permalink also for stripped-prefix supergroup ids

The `t.me/c/<id>/<msg>` link wasn't appearing on widgets in the
50-message run because the colleague's Supabase ingestion stores
supergroup chat_ids in the *stripped* form (`-2061886148`)
rather than the Bot API's `-1002061886148`. The original
`_telegram_permalink` only recognised the `> 10**12` API form
and returned `None` for everything else — including those real
supergroups — so the 🔗 line was silently dropped.

New rule:

  - `abs(chat_id) > 10**12` → Bot API form, strip the `-100`
    prefix.
  - `100_000_000 <= abs(chat_id) ≤ 10**12` → supergroup id stored
    without the prefix; use as-is.
  - `abs(chat_id) < 100_000_000` → basic group, no URL form.

Every widget for a supergroup-class chat (regardless of how the
upstream pipeline normalised the id) now carries a working
`https://t.me/c/<id>/<msg>` line.

#### FR-CR-05-16 — Unified card layout + owner deeplink

Live testing on the Edit-on-task flow exposed two visual
inconsistencies and one polish item.

**1. Unified layout (task card == widget).** The post-Accept
task card was still rendering the legacy verbose layout
(`#42 prepare deck` / `📥 backlog · 👤 Andre · 🟡 medium`), while
the FR-CR-05-13 widget had moved to the minimal layout. The
operator saw two visually different cards for the same task
across the Accept boundary. `build_task_card_text` now mirrors
`_build_draft_widget_text`:

```
{priority-emoji} <b>title</b>
📝 description
👤 <a href="tg://user?id=…">owner</a> · 📅 due
🔗 t.me/c/<chat>/<msg>
```

No `#id`, no status word, no priority word. Done tasks render
✅ in lieu of the priority circle so finished work is visually
distinct.

**2. Owner as a `tg://user?id=` deeplink.** New
`_owner_html_link(owner_user_id, display)` helper wraps the
display label in `<a href="tg://user?id=<uid>">…</a>` for
numeric Telegram user_ids. Tap on the owner = open private
chat with them. Slack uids fall through to plain text (Telegram
doesn't know them). Both the live task card and the confirm
widget use the same helper.

**3. Edit-reply name preservation.** When the operator types
«ответственный Андрей Кузьминых» and the LLM round-trips the
matching team_member id, the resolution chain previously fell
back to the raw uid (`222968032`) when the registry row was
sparse — auto-seed wrote rows with only `telegram_user_id`,
leaving `display_name` / `real_name` empty until the operator
filled them in on the Sheet. New rule: when the matched
registry row's display fields all equal the id, parse the
user's typed reply for an owner-hint pattern
(«ответственн* X», «owner X», «assign to X») and use THAT
text as `owner_display_name`. Card now renders «Андрей
Кузьминых» (hyperlinked to the resolved uid) instead of the
bare numeric id.

#### FR-CR-05-15 — Source permalink on widget, drop dialogue column

Two follow-ups after the 50-message run.

**1. 🔗 source link on every widget.** `_build_draft_widget_text`
now appends a final `🔗 <permalink>` line when
`draft.payload["_pending"]["permalink"]` is set. The permalink
is the same `t.me/c/<chat>/<msg>` deeplink the existing
`_telegram_permalink` helper builds for supergroups; private
chats and basic groups have no shareable URL and the line is
omitted gracefully. Operator can tap the link to jump straight
to the original message instead of grepping the chat history.

**2. Drop the FR-CR-05-14 `dialogue` column.** Operator
reconsidered: a full chat dialogue rendered into a single Sheets
cell is too noisy. The 23rd column is gone; `_HEADER_ROW` and
`_task_row` are back to the 22-column layout. The
`_format_dialogue` helper and the `task.extra["context_dialogue"]`
write path were removed too. The FR-CR-05-09 adaptive context
window itself is still in use — it feeds the LLM through
`history_before` for richer titles / descriptions / owner picks.
We just stopped persisting the rendered transcript per task.

**3. Same-chat context guarantee.** `recent_in_chat`'s SQL has
always carried `WHERE chat_id = :chat_id`, so cross-chat history
can never leak into the LLM's adaptive window. Pinned by
`test_recent_in_chat_filters_by_chat_id_only` so a future
refactor can't accidentally widen the query.

#### FR-CR-05-14 — Voice replies, registry-aware Edit, source-dialogue column

*(The `dialogue` column piece of this requirement was rolled
back by FR-CR-05-15; voice replies + registry-aware Edit
remain.)*

Live testing on the Edit-on-task flow surfaced three gaps; all
three fixed here.

**1. Voice messages in pending replies.** The Edit / Mark-done
reply handler accepted only plain text. A voice DM landed
without a transcript — the LLM saw an empty body, returned no
edits, the prompt stayed open. New `_maybe_transcribe_voice` on
the listener:
  - Detects `voice` / `audio` payloads on the incoming message
  - Calls `TelegramSender.download_file_bytes(file_id)` →
    `getFile` + raw GET on the resolved URL
  - Sends bytes to OpenAI Whisper via the existing
    `app/services/transcription.transcribe_bytes`
  - Returns the transcript; falls back to «🎙 Не разобрал голос»
    nudge when nothing usable came back

**2. Registry-aware Edit owner resolution.** `parse_edit_with_llm`
now accepts `known_employees` and renders a five-column table
(slack_user_id / display_name / real_name / role / notes) into
the prompt. The LLM is instructed to round-trip an id from the
table when the user names someone («ответственный Андрей
Кузьминых»). `apply_edit_reply_ex` then validates the returned
value: an id from the registry → keep with display_name
backfilled; a name → look up locally and resolve; an
unresolvable string → keep on `owner_display_name` with id
cleared. Same plumbing for `parse_draft_edit_with_llm` /
`apply_edit_draft_reply` so Edit-on-draft works the same way.

**3. `dialogue` column in Tasks Sheet.** New 23rd column carries
the adaptive-context window (FR-CR-05-09) rendered as a
plain-text «author: text» transcript. Populated at draft
creation by `_format_dialogue(history_before, source)` →
`draft.payload["context_dialogue"]` →
`task.extra["context_dialogue"]` via `create_task_from_draft`.
Caps at 8 000 chars (Sheets cell limit is 50k; leaves room for
other columns + operator notes). Read-only from the sheet's
side — operator edits are ignored on pull.

#### FR-CR-05-13 — Bot filter, sibling-draft dedup, third-party titles, widget polish

Targeted fixes after the third 100-message run.

**1. Bot account auto-deactivation.** «CEO_office1 bot» kept
landing as task owner because the seed step marked every TG
sender `active=True`. New `_looks_like_bot` heuristic in
`app/services/team_members.py` catches the common patterns —
`bot` / `_bot` suffix, ` bot` substring, `office1` /
`notif` / `support_` / `assistant_` / `webhook` /
`crm_` markers — and seeds those rows `active=False` with
`notes="auto: looks like bot account"`. They're invisible to
`as_known_employees()`, so the LLM owner stage can never pick
them. Operator can flip on the sheet if a real person was
caught (rare).

**2. Sibling-draft dedup.** `check_duplicate` now includes open
`ActionDraft(state=proposed)` rows in the lookback alongside
saved Tasks. Two adjacent source messages producing siblings of
the same task within one `prepare_drafts` batch («добавить Юру»
× 2, «организовать профиль на платформе» × 3) used to slip
through because the first draft wasn't a Task yet. Items are
now prefixed `T#` (saved task) or `D#` (pending draft) in the
LLM prompt so the model can address them distinctly. The
hallucination guard validates the returned id against the union.

**3. Third-party status promises in titles.** «Нет Алина сама
отправит» (a status sentence about another teammate's
commitment) was landing verbatim as the title. The title prompt
gains an explicit `THIRD-PARTY STATUS PROMISES` block teaching
the model to read context, identify the actual deliverable, and
write a clean imperative title — putting the original
delegation note in the description instead.

**4. Widget polish.** Per UX feedback the «📥 Create this task?»
header was redundant — the inline keyboard already says
✅ / ✏ / ✖. New layout:

```
🟠 <b>title</b>
📝 description
👤 owner · 📅 due-date
```

Priority is rendered as a single emoji next to the bold title,
no «high» / «medium» word — colour carries the signal.

#### FR-CR-05-12 — Description completeness, role-aware owner pick, passive-past detect

Targeted fixes after the second 100-message historical run.

**1. Description completeness.** The title prompt previously only
said «1-3 sentences» and trusted the LLM not to truncate. In
practice gpt-4o-mini occasionally clipped mid-sentence («так как
осталось открытым с»). New explicit rule in the prompt:
ALWAYS finish every sentence with a period; if the thought
can't fit, stop after the first complete sentence — partial
clauses are worse than a shorter description. 40-200 chars,
no markdown.

**2. Role-aware owner disambiguation.** When two teammates share
a first name (two «Алина»s), the LLM was picking alphabetically
or by position in the table — landing «Валентина» as owner of
«подать заявку на StartUp Qatar» instead of the actual Алина.
Fix: `as_known_employees()` now also surfaces `role` and `notes`
from `team_members`. The owner prompt has a new
DISAMBIGUATION section instructing the model to USE role/notes
to pick the right same-first-name match. The user-prompt table
gains role + notes columns.

**3. Passive-past status reports.** «письма в Abundance отправлены»
(passive, completed) was being captured as a task. The detect
prompt previously listed only active past examples («отправил»,
«готово»). Extended with passive forms (`отправлены`, `подписан`,
`оплачен`, `утверждён`) plus English present-perfect (`sent`,
`done`, `approved`, `signed`) and a verbatim example. These now
classify as no_action.

#### FR-CR-05-11 — Bidirectional Sheet ↔ DB sync (Tasks + Team)

Quality follow-up to FR-CR-05-10 once the Team sheet was in
operator hands. Until now the Sheet was strictly *write-only*
from the bot's perspective: every Task change pushed a row, but
operator edits on the spreadsheet died on the next push (which
overwrote them). Same story for Team. New rule: **the Sheet
wins** — operator edits propagate to the DB on the next pull
tick.

**Tasks (`Main` tab).**

- New `SheetsPullService` (`app/sync/sheets.py`). Reads every row,
  matches by `task_id` (column A), applies field-level diffs:
  - **Editable from sheet:** `title`, `description`, `owner`,
    `priority`, `category`, `start_date`, `start_time`, `due_date`,
    `due_time`, `status`, `completion_artifact`.
  - **Read-only from sheet:** `task_id`, `parent_task_id`,
    `source`, `source_permalink`, `created_at`, `updated_at`,
    `deleted_at`, `is_recurring` and the recurring-* columns
    (out of MVP scope).
- Status changes route through `TransitionService` so audit-log
  rows + subscriber notifications fire as if the change came
  from a button click. Invalid transitions log a warning and
  drop the change.
- Owner resolution accepts: bare uid (`U…` / numeric TG id),
  `@handle`, real-name match against `team_members` /
  `employees`. Unresolvable text stays in `owner_display_name`
  so the operator's intent isn't lost.
- New CLI `python -m ops.pull_tasks_sheet`. Exits 0 on success,
  2 on misconfig.
- Conflict rule: **sheet wins** within a single tick window
  (~5 min). No `updated_at` arbitration — the simplicity beats
  fighting clock skew.

**Team (`Team` tab).**

- Already bidirectional via `ops.sync_team --pull --push` from
  FR-CR-05-10. Schedule on cron at the same cadence.

**Cron** (run on the listener container or a sidecar, every 5
min — adjust as needed):

```cron
*/5 * * * *  python -m ops.pull_tasks_sheet
*/5 * * * *  python -m ops.sync_team --pull --push
```

The DB → Sheet direction stays event-driven for Tasks (the
existing `schedule_sync_task` after-commit hook pushes immediately
on every Task change), so you only need cron for the pull side
and for the Team round-trip.

#### FR-CR-05-10 — Cross-channel team registry + context-rich descriptions

Quality follow-up to FR-CR-05-09 testing on real traffic. Three
entangled changes that together turn each draft widget into a
self-contained card with a real owner and real context.

**1. Team registry as authoritative owner source.** New table
`team_members` (migration `0017`) carries one row per teammate
with both Telegram and Slack identity, role, email, active flag.
Synced bidirectionally with the `Team` tab of the spreadsheet
pointed to by `GOOGLE_TEAM_SHEETS_SPREADSHEET_ID` (falls back to
the tasks spreadsheet when only one sheet is configured).

The registry replaces «whoever is in the chat» as the owner-
universe. `_known_members_for(chat_id)` now returns
`team_members.active=True` UNION the per-chat
`telegram_chat_members` rows (de-duped by id) — team-registry
rows win because they carry the operator's curated display name /
real name.

A new owner-resolution pipeline (`_resolve_owner` in
`app/telegram_ingest/service.py`) replaces the earlier ad-hoc
chain. First-match-wins:

  1. LLM-picked `owner_user_id` resolves to a registry row → keep,
     fill display_name from the registry when missing.
  2. LLM-picked `owner_display_name` resolves to a registry row by
     name match → backfill `owner_user_id` from the row.
  3. Otherwise the display_name is DROPPED (the «CEO Rosecliff»
     killer — outsiders mentioned in chat but not on the team
     never become owners), and we fall through.
  4. Sender, but only when `_author_fallback_allowed` (FR-CR-05-09
     rule: registry empty for this chat OR sender is in it).
  5. Admin uid from `TELEGRAM_ADMIN_USER_IDS`. ALWAYS clobbers
     `owner_display_name` to the admin's registry label so a
     stale hint doesn't render alongside the admin's id.

The `Team` sheet is operator-owned. CLI:
`python -m ops.sync_team --seed --pull --push` does a one-shot
auto-seed (from `telegram_chat_members` + `employees`) +
bidirectional sync.

**2. Context-rich descriptions.** The title prompt is taught to
produce a 1-3 sentence description summarising who's involved,
what was discussed upstream, and what concretely needs to
happen — using the full adaptive-context window from FR-CR-05-09.
A vague source like «хорошо! напишу ему» with prior context «надо
ответить Андрею Соколову по сделке Acme — он спрашивал про SoW»
now yields a description like «Андрей спрашивал про SoW по сделке
Acme, нужно подготовить ответ.» rather than empty / null.

When the LLM genuinely has nothing to summarise (one-liner with
empty history), the ingest fills in a deterministic
`📝 обсуждалось в <chat_title> · <YYYY-MM-DD HH:MM>` so the
operator at least sees where the draft came from.

**3. Drop the inline-quote / forward DMs.** With the rich
description carrying context, the FR-CR-05-09 inline-quote
fallback became redundant — the operator has everything they
need on the widget itself. `post_draft_confirmation` now sends
exactly ONE DM per recipient (the widget). `_build_source_quote`
helper removed. Source text is still stashed on
`draft.payload["_pending"]["source_text"]` for any future
«show original» feature.

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

**3. Inline-quote source fallback.** *(Superseded by FR-CR-05-10
which replaced both `forwardMessage` and the inline-quote DM
with a context-rich description on the widget itself.)*
`post_draft_confirmation` originally tried `forwardMessage` first,
but the Bot API only forwards messages the bot had observed via
`getUpdates` — every historical migration draft failed forward.
The prepare-drafts step pre-stashed `source_text` on
`draft.payload["_pending"]` so the card helper could emit a
`<blockquote>`-wrapped HTML quote when the forward returned
empty. The mechanism worked but produced two DMs per recipient;
FR-CR-05-10 collapsed it to a single widget message whose
`description` already carries the chat context. The
`source_text` field is still stashed on every draft for any
future «show original» feature.

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
| FR-CR-05-09  | `test_telegram_ingest.py::test_build_window_carries_history_before` (history_before threads through to the ContextWindow); `::test_process_all_falls_back_to_admin_when_owner_unresolved` + `::test_process_all_keeps_real_member_sender_as_owner` (later refactored into the FR-CR-05-10 `_resolve_owner` chain); `::test_prepare_drafts_stashes_source_text_for_quote_fallback` (`_pending["source_text"]` still populated as a safety net for any future «show original» feature, even though FR-CR-05-10 superseded the inline-quote DM with a rich description); `::test_recent_in_chat_returns_chronological_with_char_cap` + `::test_recent_in_chat_expands_in_steps_when_under_threshold` (10→20→30 expansion until ~10k chars); `test_intent_pipeline.py::test_detect_prompt_lists_status_reports_and_parroted_phrases_as_no_action` + `::test_title_prompt_teaches_imperative_rewrite_from_context` (prompt content pinned) |
| FR-CR-05-10  | `test_team_members.py` (read paths, prefer-telegram id selection, find-by helpers; `seed_from_chat_members` / `seed_from_slack_employees` idempotent + bot-skip; sheet round-trip headers, insert-then-update-by-id, match-by-tg-id-when-no-id, active-bool normalisation incl. `да` / `yes` / `1` and empty→true default); `test_telegram_ingest.py::test_resolve_owner_kills_unknown_display_name_and_falls_back_to_admin` (the «CEO Rosecliff» killer — unresolvable display_name dropped, owner = admin, display = admin's registry label); `::test_resolve_owner_keeps_real_team_member` (LLM-picked `owner_user_id` matching a registry row stays, display_name backfilled); `::test_resolve_owner_resolves_display_name_via_registry` (name-only LLM hint → registry lookup → numeric id); `::test_prepare_drafts_fills_in_fallback_description_when_llm_silent` («обсуждалось в <chat> · <YYYY-MM-DD HH:MM>» when LLM produced no description); `::test_prepare_drafts_keeps_llm_description_when_present` (real LLM description not clobbered); `test_telegram_cards.py::test_post_draft_confirmation_sends_only_widget_no_forward_no_quote` (FR-CR-05-09 inline-quote DM removed — widget itself carries context via description); `test_telegram_listener.py::test_listener_routes_group_messages_to_draft_flow` updated for «no forward» |
| FR-CR-05-14  | `test_telegram_listener.py::test_maybe_transcribe_voice_returns_text_for_text_message` (text replies skip transcription); `::test_maybe_transcribe_voice_returns_empty_when_no_voice_no_audio` (no attachment ⇒ empty); `::test_maybe_transcribe_voice_calls_whisper_with_downloaded_bytes` (voice payload ⇒ download via sender + Whisper round-trip); `::test_maybe_transcribe_voice_skips_when_openai_key_missing` (no OPENAI_API_KEY ⇒ no download attempt); `test_telegram_conversations.py::test_parse_edit_with_llm_includes_known_employees_in_prompt` (5-col registry table rendered into the Edit prompt); `::test_apply_edit_resolves_owner_name_via_team_registry` (LLM-returned name «Андрей Кузьминых» ⇒ owner_user_id resolved against team_members + display_name backfilled); `::test_apply_edit_drops_unresolvable_owner_text_to_display_name` (unresolvable text kept on owner_display_name, owner_user_id cleared). The original `test_task_row_includes_dialogue_from_extra` / `_dialogue_empty_when_no_extra` tests were rolled back by FR-CR-05-15. |
| FR-CR-05-15  | `test_telegram_cards.py::test_draft_widget_text_includes_source_permalink_when_available` (🔗 line carries `t.me/c/<chat>/<msg>` when `_pending["permalink"]` is set); `::test_draft_widget_text_omits_link_line_when_no_permalink` (no empty 🔗 line for private DMs / basic groups); `test_sheets_pull.py::test_task_row_does_not_include_dialogue_column` (22-column header restored, last column is `completion_artifact`); `test_telegram_ingest.py::test_recent_in_chat_filters_by_chat_id_only` (adaptive context window is per-chat — SQL `WHERE chat_id = :chat_id` pinned so a future refactor can't widen the query) |
| FR-CR-05-23  | `test_team_members.py::test_backfill_fills_blank_team_members_from_chat_members` (sparse rows enriched from listener observations; operator edits preserved); `::test_backfill_no_op_when_chat_members_empty` (no observations ⇒ no rows changed) |
| FR-CR-05-30  | manual visual verification — listener logs `listener_team_sheet_pulled` only when the operator actually changed something on the Sheet (no timestamp-only diffs) |
| FR-CR-05-31  | `test_intent_pipeline.py::test_owner_prompt_uses_role_notes_for_unnamed_assignments` (system prompt has SOURCE OF TRUTH block + investor-relations example anchored); `::test_owner_user_prompt_keeps_long_notes_intact` (200-char notes cap; previously-clipped «cap-table / встречи с инвесторами» blurbs survive into the prompt) |
| FR-CR-05-32  | `test_telegram_conversations.py::test_format_edit_receipt_lists_applied_fields` (icon + arrow rendering for each field; empty values skipped); `::test_format_edit_receipt_hints_at_vague_owner_without_match` («другого оунера» without a resolved owner ⇒ clarification nudge appended); `::test_format_edit_receipt_no_vague_hint_when_owner_resolved` (no nudge when the LLM did pick an owner) |
| FR-CR-05-33  | `test_telegram_cards.py::test_render_tombstone_resolves_actor_uid_to_team_name` (session + matching team_members row ⇒ tombstone shows real_name, no raw uid); `::test_render_tombstone_falls_back_to_uid_without_session` (legacy callers without a session keep the raw uid behaviour) |
| FR-CR-05-34  | `test_telegram_bot.py::test_confirm_keyboard_has_three_buttons_in_order` (order pinned as `[ignore, edit, confirm]`); manual verification that `_looks_like_confirm_widget` now accepts either order so widgets in flight from before the upgrade still route Edit clicks correctly |
| FR-CR-05-35  | `test_telegram_listener.py::test_listener_view_realtime_off_by_default` (flag off ⇒ reader.iter_newest never called); `::test_listener_view_realtime_pulls_when_enabled` (flag on ⇒ listener pulls + posts widget DM via `prepare_drafts` / `post_draft_confirmation`); `::test_listener_view_realtime_throttled_within_interval` (repeated calls inside the window are no-ops); `::test_listener_view_realtime_no_op_when_reader_unconfigured` (no source URL ⇒ silent no-op even with the flag on) |
| FR-CR-05-36  | `test_telegram_listener.py::test_listener_view_realtime_pulls_full_batch_size_per_poll` (single SQL roundtrip per poll, limit = `view_poll_batch_size`; 500 default covers realistic bursts) |
| FR-CR-05-37  | `test_telegram_conversations.py::test_prompt_done_returns_text_for_owner` (prompt invites optional reply, no «/skip»); `::test_apply_done_no_op_when_reply_empty` (empty reply is a no-op now that the transition happened on click); `::test_apply_done_url_artifact` + `::test_apply_done_text_artifact` (artifact still stored when the operator does reply, with no extra transition attempt) |
| FR-CR-05-112 | `test_intent_pipeline.py::test_has_explicit_temporal_anchor_helper` (Russian + English deadline anchors → True; meeting-slot dates без «к/до/by» → False; empty / None safe). Manual: `node_date` drops LLM-picked due_date when no anchor in source; logged as `date_node_dropped_no_temporal_anchor`. |
| FR-CR-05-111 | `test_task_dedup.py::test_dedup_similar_description_safety_net_skips_llm` (Beta-Jared Zoom-ID dup → similarity≥0.70 fast path catches it without LLM call); `::test_dedup_similarity_does_not_match_unrelated_descriptions` (different work → similarity gate doesn't fire, LLM still called) |
| FR-CR-05-110 | `test_task_dedup.py::test_dedup_dispatches_to_llm_with_full_descriptions` updated for the FR-CR-05-110/111 fast-path bypass; existing dedup tests still hold; `Settings.dedup_fast_path` config flag wired to enable/disable the safety net |
| FR-CR-05-109 | Existing `test_intent_pipeline.py::test_detect_node_python_guard_rejects_transcript_prefix_sources` still pins the guard but extended with reflection patterns; manual: `_resolve_uids_in_text` now emits «Name (uid)» format; `_annotate_uids` runs in `prepare_drafts` before `_build_window` |
| FR-CR-05-108 | `test_intent_pipeline.py::test_detect_node_python_guard_rejects_transcript_prefix_sources` («На изображени*», «На скрин*», «На фото», «Обсужда*т», «В переписк*», «По переписк*», «Сообщени* от», «In the image», «This screenshot shows», «On the screen» all match; real tasks pass through; None/empty safe); manual: removed last two `_fallback_description` call sites — `prepare_drafts` and `process_all` now both rely on the FR-CR-05-105 «empty description → drop draft» guard; `pipeline.py::node_owner` emits `owner_node_result` log line |
| FR-CR-05-107 | `test_llm_backends.py::test_openai_call_uses_completion_tokens_for_gpt5` (also asserts `temperature` not in kwargs); `::test_openai_call_uses_max_tokens_for_gpt4o` (asserts `temperature=0` survives for gpt-4o); `::test_openai_complete_text_drops_temperature_for_gpt5`; `::test_openai_complete_text_keeps_temperature_for_gpt4o` |
| FR-CR-05-106 | `test_llm_backends.py::test_model_uses_completion_tokens_helper` (gpt-5/o1/o3/o4 → True; gpt-4o/4-turbo/3.5-turbo → False); `::test_openai_call_uses_completion_tokens_for_gpt5` (gpt-5.5 call ships `max_completion_tokens=4096`, no `max_tokens`); `::test_openai_call_uses_max_tokens_for_gpt4o` (gpt-4o still ships `max_tokens=4096`) |
| FR-CR-05-105 | `test_telegram_ingest.py::test_prepare_drafts_drops_when_llm_returns_empty_description` (LLM returns no description → draft is deleted, no fallback template); `::test_prepare_drafts_keeps_llm_description_when_present` still pins the kept-as-is path |
| FR-CR-05-104 | `test_intent_pipeline.py::test_detect_prompt_rejects_chat_opener_retrospective_recap` (Chat-opener + retrospective recap block + GP Morgan regression + Russian/English fillers pinned); `::test_default_models_use_gpt_5_5_everywhere` (all six model fields default to gpt-5.5); `test_llm_backends.py::test_settings_defaults_for_openai_model` (asserts gpt-5.5 default); `test_task_dedup.py::test_dedup_call_uses_strong_model` (asserts dedup call passes `model=gpt-5.5`) |
| FR-CR-05-103 | `test_intent_pipeline.py::test_strip_chat_prefix_to_imperative_handles_operator_regression` (Neuberger «@IrinaMorato подскажи, пожалуйста, отправить фоллоу-ап Neuberger ?» → «Отправить фоллоу-ап Neuberger»; multi-mention; English variant; пожалуйста-only; pure-wrapper falls to naked verb downstream); `::test_title_prompt_pins_chat_question_to_imperative_rule` (CHAT-QUESTION REQUESTS block + worked rewrite pinned in TITLE_SYSTEM_PROMPT) |
| FR-CR-05-102 | `test_task_dedup.py::test_dedup_call_uses_strong_model` (dedup `call_tool` invoked with `model="gpt-4o"`, not the default mini); existing `::test_dedup_dispatches_to_llm_with_full_descriptions` still pins the ≤1500-char description feed |
| FR-CR-05-101 | `test_task_dedup.py::test_dedup_prompt_is_minimal_focused_classifier` (≤700-char final form: 10-existing framing + «compare descriptions» + output schema); `test_intent_pipeline.py::test_dedup_prompt_minimal_no_legacy_blocks` (legacy synonym/family blocks gone); `test_intent_graph.py::test_date_node_does_not_fall_back_when_llm_intentionally_null` (LLM null + reasoning → no fallback); `::test_date_node_falls_back_when_llm_silent_no_reasoning` (still falls back when call genuinely failed) |
| FR-CR-05-100 | `test_intent_pipeline.py::test_title_prompt_converts_first_person_to_imperative` (FIRST-PERSON COMMITMENTS block + Артём Барсуков regression «Я тебе сейчас пришлю драфт письма» → «прислать драфт письма по Артему Барсукову» rewrite + «Я отправлю» / «I'll send» / «сейчас скину» fragments pinned); `test_task_dedup.py::test_dedup_dispatches_to_llm_with_full_descriptions` (no deterministic gate; LLM sees up to 1500 chars of description for both candidate and existing); `::test_dedup_prompt_is_minimal_focused_classifier` (≤2500-char prompt + «look at the descriptions, not just the titles» framing) |
| FR-CR-05-99  | `test_task_dedup.py::test_dedup_prompt_is_minimal_focused_classifier` (≤1700-char tight binary-classifier prompt with verb-family + specific-subject + EXTERNAL-audience-discriminator + Default-to-FALSE rules); `::test_dedup_prompt_keeps_different_audience_distinct` («отчёт Ирине ≠ отчёт Артёму» rule survives the minimal-prompt rewrite); `test_intent_pipeline.py::test_is_naked_verb_title_catches_bare_verbs` (Russian + English bare-verb list; trailing punctuation stripping; verb-with-object passes through); `test_telegram_listener.py::test_listener_tick_processes_updates_and_advances_offset` updated for dedup behaviour (2 same-title updates → 1 Task created via deterministic dedup, both bookmarked) |
| FR-CR-05-98  | `test_task_dedup.py::test_dedup_prompt_pins_one_event_collapse_rule` (ONE-EVENT COLLAPSE block + Ryan Gariepy «Организовать» vs «Пригласить Йохана» worked counter-example + «single named external event» discriminator + «distinct deliverable» escape hatch all pinned) |
| FR-CR-05-97  | `test_task_dedup.py::test_normalize_title_for_match_collapses_whitespace_case_yo_e` (case + whitespace + leading-trailing-punctuation + ё↔е); `::test_dedup_deterministic_match_skips_llm` (Atuwatse Okorodudu regression: identical title triple-match → is_duplicate=true, LLM never called); `::test_dedup_deterministic_match_normalises_case_and_punctuation` («  подтвердить  ВСТРЕЧУ.  » matches «Подтвердить встречу»); `::test_dedup_deterministic_does_not_match_when_owner_differs` (different owner → falls through to LLM) |
| FR-CR-05-96  | `test_task_dedup.py::test_dedup_prompt_pins_meeting_family_and_confirm_family` (5 family names + 7 individual synonyms + 4 worked counter-examples pinned) |
| FR-CR-05-95  | `test_intent_pipeline.py::test_detect_prompt_rejects_third_party_future_intent` («Они сами отправят», «Артем сам пришлёт», «They will send the link themselves» pinned); `::test_detect_prompt_rejects_emotional_chat_outbursts` («Очень важный день», «помолиться» pinned); `::test_dedup_prompt_pins_synonym_verbs_and_same_subject` (3 regression pairs + synonym families pinned); `::test_normalize_task_title_caps_at_80_chars` (hard cap 80, clause-break uses `. ` for the «Очень важный день. …» split) |
| FR-CR-05-94  | `test_intent_pipeline.py::test_detect_prompt_rejects_opinion_qualifier_statements` (Singapore/HK 250-char regression + «По X я не против, но Y» / «Они у Алины в задачах есть» / «Мне кажется» / «I think we should» fragments + FR-CR-05-94 pinned); `::test_date_prompt_status_as_of_is_not_a_deadline` («статус на 26/02» pattern + 2027-02-23 BAD-output + status-update framing); manual verification: drafts now show ≤101-char titles via `normalize_task_title` in `prepare_drafts`; `_resolve_uids_in_text` resolves bare 9-15 digit tokens to `team_members.real_name`; edit prompt accepts `current_user_id` + `current_user_label` so «на меня» resolves to the editor's uid; `ops/wipe_tasks.py --also-wipe-sheet` calls `values().clear(A2:V)`. |
| FR-CR-05-93  | `test_intent_pipeline.py::test_detect_prompt_rejects_uzhe_completed_recap_as_no_action` («уже / already» prefix + «уже написала» / «уже отправила» / «Уже написала на почту» / «и инвайт отправила» fragments + FR-CR-05-93 pinned) |
| FR-CR-05-92  | `test_task_dedup.py::test_dedup_prompt_pins_transliteration_rule` (TRANSLITERATION block + James Morgon / Джеймсу Моргану / Olayan / Олаян / Артем / Артём / Artem / Petya / Петя fragments + FR-CR-05-92 pinned) |
| FR-CR-05-91  | `test_wipe_tasks_cli.py::test_wipe_dry_run_keeps_all_rows`; `::test_wipe_without_yes_flag_is_dry_run` (no `--yes` → no deletion); `::test_wipe_with_yes_clears_task_data_keeps_team_registry` (tasks/drafts/history/subs/sheets-sync/audit gone, team_members + telegram_chat_members preserved); `test_evening_status.py::test_evening_tomorrow_plan_drops_owner_badge_when_recipient_is_owner`; `::test_evening_admin_tomorrow_plan_groups_per_person` (per-person sections with `👤 <Name>` headers + counts); `::test_evening_admin_audit_payload_carries_per_person_plan_task_ids` (audit row carries `{uid: [task_id]}` for morning diff); `test_morning_cards.py::test_morning_admin_diff_renders_added_and_done_per_person` (✅ done + 🗑 deleted + ➕ added classification); `::test_morning_admin_diff_returns_none_with_no_changes`; `::test_morning_admin_diff_returns_none_when_no_prior_plan` |
| FR-CR-05-90  | `test_retro_share_docs_cli.py::test_retro_share_dry_run_skips_api_calls` (--dry-run never calls `_share_anyone_with_link`); `::test_retro_share_invokes_share_with_writer_role_by_default` (one call per recording, role=writer); `::test_retro_share_role_flag_overrides_default` (--role reader → reader); `::test_retro_share_skips_recordings_without_google_doc_id` (null doc_id excluded by SQL filter); `::test_retro_share_continues_on_per_doc_failure` (per-doc 4xx doesn't abort, exit=1); `::test_retro_share_returns_2_when_credentials_unavailable` (bad config → exit 2); `::test_fireflies_pipeline_calls_export_summary_with_writer_default` (invariant — pipeline does NOT pass `share_role=` to export_summary, default 'writer' wins) |
| FR-CR-05-89  | `test_resync_sheet_cli.py::test_normalize_task_title_caps_long_no_break_paragraph` (200-char paragraph with no early break → 100+ellipsis word-boundary cut); `::test_normalize_task_title_first_clause_break_wins_over_hard_cut` («Поговорил с Fortuna: …» kept first clause); `::test_normalize_task_title_preserves_short_titles_unchanged` (no rewriting on short input); `::test_resync_sheet_dry_run_reports_capped_titles_without_writing`; `::test_resync_sheet_caps_titles_resets_row_id_and_resyncs` (title normalised in DB, row_id cleared, sheets.sync invoked); `::test_resync_sheet_include_deleted_flag_pushes_tombstones`; `::test_resync_sheet_returns_exit_code_2_when_no_credentials`; `test_intent_pipeline.py::test_detect_prompt_rejects_transcription_dumps_as_no_action` (TRANSCRIPTION DUMP HARD RULE + «На изображении» / «Обсуждают» / «Это что?» worked failure-modes pinned); `::test_title_prompt_forbids_naked_verb_titles` («Встретиться» counter-example + (уточнить детали) fallback); `::test_title_prompt_pins_truncated_date_range_failure» («Ryan будет в Лондоне с 4 по» half-range example); `::test_date_prompt_requires_proof_quote_or_null` (PROOF QUOTE OR NULL rule + MGX «до конца мая» counter-example + «либо в описание добавляй пруф либо сегодня» literal phrase pinned) |
| FR-CR-05-83  | `test_evening_status.py::test_evening_tomorrow_plan_lists_tasks_for_next_day` (status digest + second message naming tomorrow's tasks; `tg://openmessage` hyperlink lands on the title); `::test_evening_tomorrow_plan_includes_overdue_today` (🚨 bullet on overdue lines + «Rolling over from today: 1» count); `::test_evening_tomorrow_plan_skipped_when_no_tasks` (no second DM when nothing scheduled, `tomorrow_plans_sent=0`); `::test_evening_tomorrow_plan_long_list_splits_into_multiple_messages` (80-task list splits at task boundaries, every chunk ≤4096 chars, `(continued)` marker on follow-ups); `::test_evening_status_groups_done_in_progress_todo` updated to assert 2 DMs (status + plan) |
| FR-CR-05-84  | `test_morning_cards.py::test_morning_cards_records_card_messages_in_audit_payload` (audit row carries `[{chat_id,message_id}…]` for intro + every card); `::test_morning_cards_deletes_yesterdays_cards_before_posting_today` (day-2 run calls `deleteMessage` on every prior-day card BEFORE posting today's intro; `prior_cards_deleted=2`); `::test_morning_cards_no_prior_audit_row_means_no_delete_calls` (first-ever run = no deletes); `::test_morning_cards_delete_failures_dont_abort_today_post` (Telegram-refuses-to-delete failures swallowed, today's posting continues) |
| FR-CR-05-85  | `test_telegram_digest_cron.py::test_cron_registry_includes_evening_and_morning_flows` (both subtypes present); `::test_cron_registry_evening_routes_to_send_evening_status_report` + `::test_cron_registry_morning_routes_to_send_morning_task_cards` (function identity pinned); `::test_cron_registry_lists_all_expected_subtypes` (full set); `::test_cron_main_passes_iso_date_through_to_evening` + `::test_cron_main_passes_iso_date_through_to_morning` (`--date` parses to ISO and reaches the called fn as `today=`); `::test_cron_main_only_builds_llm_backend_for_evening_flow` (morning never touches `_build_llm_backend`) |
| FR-CR-05-86  | `test_sheets_sync_hooks.py::test_col_letter_helper_handles_az_and_aa_boundaries` (`_col_letter` 1→'A', 22→'V', 26→'Z', 27→'AA', 53→'BA'); `::test_append_uses_schema_width_range_not_a_z` (`range="Main!A:V"` not `A:Z`); `::test_update_uses_schema_width_range_not_a_z` (`A42:V42` not `A42:Z42`) |
| FR-CR-05-87  | `test_intent_pipeline.py::test_date_prompt_requires_date_to_belong_to_task_action` (rule 10 + FR-CR-05-87 + the MGX «до конца мая» / `2026-05-31` BAD-output worked example pinned; «which verb does the date modify?» discriminator + «if unsure, EMIT NULL» fallback all enforced) |
| FR-CR-05-88  | `test_intent_pipeline.py::test_title_prompt_forbids_titles_ending_in_preposition` (NEVER END A TITLE WITH A PREPOSITION block + «Спросить слоты с» worked counter-example + Russian + English preposition lists pinned); `::test_title_prompt_forbids_null_description_with_context_present` (NEVER RETURN NULL DESCRIPTION block + «Исправлено, отправлять?» / MGX letter / «Artem/Alina/Irina» fragments + «≥1 prior context message ⇒ ≥2 sentences» rule pinned) |
| FR-CR-05-82  | `test_intent_pipeline.py::test_title_prompt_requires_named_entity_coverage` (NAMED-ENTITY COVERAGE block + Fubon / Ryan Gariepy / May 6 11:30 London / FR-CR-05-82 fragments pinned; entity classes spelled out); `::test_title_prompt_length_rule_targets_3_to_6_sentences` («3-6 sentences» replaces «1-3 SHORT sentences»); `::test_date_prompt_forbids_implicit_monday_inference` (NO HALLUCINATING DATES + four required literal triggers + multi-candidate-dates rule + 2026-05-04 BAD-output regression all pinned, «never pick the earliest» enforced) |
| FR-CR-05-38  | manual visual verification — after an Edit reply the listener posts a fresh DM with the full rendered task card / widget body in context; the original card / widget is also edited in place by `refresh_card` / `refresh_draft_widgets` |
| FR-CR-05-40  | `test_evening_status.py::test_evening_status_groups_done_in_progress_todo` (3 tasks → 3 sections + 1 LLM call each); `::test_evening_status_subscriber_only_user_still_gets_dm` (no owned tasks but subscribed → 👀 Подписки section); `::test_evening_status_skips_user_with_no_tasks` (no tasks → 0 recipients); `::test_compose_narrative_falls_back_when_llm_raises` + `::test_compose_narrative_falls_back_when_llm_returns_empty` (fail-open to deterministic fallback); `::test_compose_narrative_truncates_long_response` (LLM > 240 chars → trimmed + `…`); `::test_evening_status_works_without_llm` (`llm=None` still ships fallback); `::test_evening_status_renders_title_as_hyperlink` (`<a href=permalink><b>title</b></a>`); `::test_evening_status_idempotent_per_user_per_day` (re-run = no new DMs); `::test_evening_status_admin_gets_team_overview` (admin uid → «Сводка по команде» with ALL tasks); `::test_split_groups_packs_into_multiple_messages_under_cap` + `::test_split_groups_single_message_when_short` (helper unit tests); `::test_evening_status_splits_long_report_into_multiple_messages` (80 tasks + verbose narrative → ≥2 DMs, each ≤4096 chars) |
| FR-CR-05-41  | `test_morning_cards.py::test_morning_cards_picks_due_today_in_progress_and_current_week_no_due` (selector: 3 categories qualify; far-away + done excluded); `::test_morning_cards_orders_by_priority_then_due_time` (urgent → high-early → med-late → low); `::test_morning_cards_subscriber_gets_separator_only_when_owned_above` (📋 / 👀 boundary marker only when there's something above it); `::test_morning_cards_attaches_full_task_keyboard` (Mark done + Edit buttons present on the in_progress card); `::test_morning_cards_intro_lists_day_count` (intro DM has «Доброе утро» + N); `::test_morning_cards_idempotent_per_user_per_day` (re-run = no-op); `::test_morning_cards_owner_with_no_due_today_marked_skipped` (no qualifying tasks → skipped_no_tasks=1) |
| FR-CR-05-39  | `test_fireflies.py::test_client_disabled_when_token_empty` (empty `FIREFLIES_API_TOKEN` ⇒ client.enabled=False, list_transcripts=[]); `::test_client_parses_graphql_transcripts_payload` (GraphQL response → `FirefliesTranscript`, unix-millis date, attendee shapes, Bearer auth header); `::test_client_handles_empty_response` (empty GraphQL body degrades to []); `::test_pipeline_process_one_runs_every_step` (every step lands an artefact + flips its flag, audio file lands on disk, tasks created with `source_kind=fireflies` + `due_date=date.today()` + admin attribution); `::test_pipeline_idempotent_when_already_processed` (re-run returns `skipped_reason='already_processed'` and creates no new tasks); `::test_pipeline_admin_fallback_for_unresolved_owner` (LLM null/hallucinated owner ⇒ admin uid wins via FR-CR-05-09 fallback); `::test_truncate_caps_at_limit` + `::test_truncate_passthrough_when_short` (2000-char hard cap on short summary); `test_task_source_kind.py::test_source_kind_enum_values` (enum carries `slack`, `telegram`, `fireflies`) |
| FR-CR-05-29  | `test_team_members.py::test_upsert_from_sheet_rows_merges_duplicates_by_unique_column` (operator edits one row to carry BOTH `telegram_user_id` AND `slack_user_id` ⇒ orphan row that previously owned one of those ids gets deleted; pull lands cleanly without `UniqueViolation`) |
| FR-CR-05-28  | `test_telegram_listener.py::test_listener_runs_sheet_pulls_when_interval_elapsed` (first call after construction fires both pulls); `::test_listener_throttles_sheet_pulls_within_interval` (repeated calls inside the window are no-ops); `::test_listener_skips_sheet_pulls_when_interval_zero` (`SHEET_POLL_INTERVAL_SECONDS=0` disables the in-listener poll); `::test_listener_swallows_sheet_pull_errors` (transient HTTP errors don't break the listener) |
| FR-CR-05-27  | `test_telegram_members.py::test_upsert_member_creates_team_row_for_new_user` (brand-new user observed ⇒ team_members row auto-created with all available fields, `active=True`); `::test_upsert_member_creates_inactive_team_row_for_bot_account` (auto-bot detection ⇒ `active=False` on creation); `test_team_members.py::test_team_sheet_push_appends_only_new_rows` (existing operator edits preserved; only DB rows missing from the sheet get appended); `::test_team_sheet_push_writes_full_table_when_sheet_empty` (first-time bootstrap writes header + body) |
| FR-CR-05-26  | `test_telegram_bot.py::test_build_task_card_text_renders_underscore_username_as_plain_html` (`@handle` display ⇒ visible label is the bare handle, hyperlinked); `::test_build_task_card_text_renders_plain_text_when_no_username_no_session` (no session + no `@` ⇒ plain text, no `tg://user?id=` fallback); `::test_build_task_card_text_links_owner_via_at_handle_when_no_numeric_id` (Slack uid + `@handle` ⇒ `https://t.me/<handle>`); `::test_build_task_card_text_renders_plain_text_when_no_username_anywhere` (registry has real_name but no username ⇒ plain real-name); `::test_build_task_card_text_renders_telegram_user_id_when_no_real_name` (no real_name anywhere ⇒ visible label is numeric uid, still no link); `test_telegram_cards.py::test_draft_widget_text_renders_plain_text_when_no_username_anywhere` + `::test_draft_widget_text_renders_owner_as_tme_link_when_username_in_registry` (same rules on the confirm widget; registry's real_name wins over LLM's short form) |
| FR-CR-05-25  | `test_telegram_ingest.py::test_map_row_extracts_dedicated_username_column` (`sender_username` ⇒ `TelegramSourceMessage.username`, leading @ stripped); `::test_map_row_extracts_message_link_as_permalink` (`message_link` column ⇒ `TelegramSourceMessage.permalink`); `::test_telegram_permalink_prefers_view_supplied_link` (`_telegram_permalink` returns the view's URL when set, even for chat shapes where reconstruction would return None); `test_team_members.py::test_seed_from_telegram_source_pulls_distinct_users` (modern view shape — both real_name and username populated cleanly; legacy heuristic still works for views without the column) |
| FR-CR-05-24  | `test_team_members.py::test_enrich_from_bot_api_populates_blank_fields` (Bot API getChat result populates blank username / real_name; rows already populated are skipped without calls); `::test_enrich_from_bot_api_silently_skips_unknown_users` (getChat returns `{}` ⇒ row stays sparse, no crash); `::test_enrich_from_bot_api_noop_when_sender_disabled` (no token / sender ⇒ early-return) |
| FR-CR-05-22  | `test_intent_pipeline.py::test_title_prompt_forbids_vague_placeholder_phrases_in_description` (CONCRETE OVER VAGUE block + concrete examples «указанных людей» / «правильной командой» / «the right people» pinned; «(уточнить)» fallback when context lacks names); `::test_title_prompt_forbids_first_person_plural_in_description` (THIRD PERSON block + «нам» / «будем рады» / «we'd love» pinned) |
| FR-CR-05-21  | `test_telegram_ingest.py::test_resolve_owner_registry_display_wins_over_llm_short_form` (LLM extracted «Артем», registry has «Артем Соколов» ⇒ registry wins); `test_telegram_members.py::test_upsert_member_enriches_sparse_team_members_row` (listener observation populates blank `telegram_username` / `real_name` on the matching `team_members` row); `::test_upsert_member_does_not_overwrite_operator_edits` (operator-edited fields are preserved); `::test_upsert_member_no_team_row_is_a_noop` (users without a team row stay only in chat_members) |
| FR-CR-05-20  | `test_telegram_bot.py::test_build_task_card_text_resolves_owner_link_via_team_registry` (registry has both numeric id + handle ⇒ public `t.me/<handle>` wins over `tg://user?id=`); `::test_build_task_card_text_falls_back_to_chat_members_for_username` (team_members row has only the numeric id but `telegram_chat_members` has the `@handle` ⇒ adopt the chat-members username); `::test_build_task_card_text_falls_back_to_tg_user_id_when_no_handle_anywhere` (no handle in either table ⇒ last-ditch `tg://user?id=` link) |
| FR-CR-05-19  | `test_telegram_bot.py::test_build_task_card_text_resolves_owner_link_via_team_registry` (real-name display + Slack uid + registry row with `telegram_user_id` ⇒ `tg://user?id=…` deeplink); `::test_build_task_card_text_falls_back_to_handle_when_registry_has_only_username` (registry row with only `telegram_username` ⇒ `https://t.me/<handle>` link) |
| FR-CR-05-18  | `test_telegram_bot.py::test_build_task_card_text_wraps_title_in_source_link` (title becomes `<a href=permalink><b>title</b></a>`); `::test_build_task_card_text_falls_back_to_plain_bold_without_permalink` (no permalink ⇒ plain `<b>title</b>`, no broken `<a href="">`); `::test_build_task_card_text_links_owner_via_at_handle_when_no_numeric_id` (Slack uid + `@handle` display ⇒ `https://t.me/<handle>` link); `test_telegram_cards.py::test_draft_widget_text_wraps_title_in_source_permalink` (same on the confirm widget); `::test_draft_widget_text_falls_back_to_plain_bold_without_permalink` (private DM / basic group fallback) |
| FR-CR-05-17  | `test_telegram_ingest.py::test_telegram_permalink_for_supergroup_with_api_prefix` (`-100` Bot API form ⇒ stripped); `::test_telegram_permalink_for_supergroup_without_prefix` (`-2061886148` Supabase form ⇒ used as-is); `::test_telegram_permalink_returns_none_for_basic_group` (small negative id ⇒ no URL); `::test_telegram_permalink_returns_none_for_private_chat` (positive chat_id ⇒ no URL) |
| FR-CR-05-16  | `test_telegram_bot.py::test_build_task_card_text_uses_minimal_layout_no_id_no_status_no_priority_word` (live task card matches the widget — no `#id`, no status word, no priority word); `::test_build_task_card_text_marks_done_with_check_emoji` (✅ replaces the priority circle on `done`); `::test_build_task_card_text_renders_owner_as_tg_user_link` + `::test_build_task_card_text_skips_link_for_slack_uid` (numeric TG uid → `tg://user?id=` hyperlink; Slack `Uxxx` falls through to plain text); `::test_build_task_card_text_includes_source_link_when_set` (🔗 deeplink on the live task card too, not just the draft widget); `test_telegram_cards.py::test_draft_widget_text_renders_owner_as_tg_user_link` (same hyperlink helper used in the confirm widget); `test_telegram_conversations.py::test_apply_edit_keeps_typed_name_when_registry_row_is_sparse` (operator types «ответственный Андрей Кузьминых» + sparse registry row ⇒ `owner_display_name` keeps «Андрей Кузьминых», not the raw uid) |
| FR-CR-05-13  | `test_team_members.py::test_looks_like_bot_heuristics` (`bot` / `_bot` / `office1` / `support_` markers caught; «Bobotov» / «Алина» pass through unflagged); `::test_seed_from_chat_members_marks_bot_accounts_inactive` (auto-seed leaves obvious bot rows `active=False` so they never enter the owner-candidate list); `test_task_dedup.py::test_dedup_lookback_includes_open_action_drafts` (proposed `ActionDraft` rows show up in the lookback with `D#`-prefix; sibling drafts within one batch dedup); `::test_dedup_invented_id_dropped_when_drafts_in_lookback` (hallucination guard validates against the Task ∪ Draft id union); `test_intent_pipeline.py::test_title_prompt_forbids_third_party_status_promises` («Нет Алина сама отправит» pinned as a forbidden title with a context-driven imperative rewrite as the example); `test_telegram_cards.py::test_draft_widget_text_drops_create_header_and_uses_emoji_only_priority` (no «📥 Create this task?» header, first line is `priority-emoji <b>title</b>`, no «high» / «medium» word in the body) |
| FR-CR-05-12  | `test_intent_pipeline.py::test_detect_prompt_rejects_passive_past_tense_status_reports` (passive forms `отправлены` / `подписан` / `оплачен` / `утверждён` + EN `sent` / `approved` listed; concrete «письма в Abundance отправлены» example pinned); `::test_title_prompt_forbids_trailing_clauses_in_descriptions` (LENGTH RULE block + «complete sentence» / «trail off» language); `::test_owner_prompt_renders_role_and_notes_columns` (role + notes columns rendered in the user-prompt table); `::test_owner_prompt_disambiguation_section_lists_role_first` (system prompt has a DISAMBIGUATION block teaching the LLM to USE role / notes); `test_team_members.py::test_as_known_employees_carries_role_and_notes` (registry rows feed role + notes into the LLM's candidate list) |
| FR-CR-05-11  | `test_sheets_pull.py::test_pull_applies_editable_fields_only` (title / description / priority / category / start+due dates+times / completion_artifact picked up; read-only columns ignored); `::test_pull_routes_status_change_through_transition_service` (status flip emits a TaskStatusHistory row via TransitionService); `::test_pull_drops_invalid_priority_silently` (`urgent!` ignored, no exception); `::test_pull_drops_invalid_status_transition` (todo→done shortcut without LLM still allowed; nonsense statuses logged + skipped); `::test_pull_resolves_owner_by_uid_handle_and_realname` (bare uid kept, `@handle` resolves via team_members.telegram_username, real-name resolves via team_members.real_name → telegram_user_id); `::test_pull_keeps_unresolvable_owner_text_as_display_name` (operator's typed «John from Acme» preserved on `owner_display_name`); `::test_pull_skips_soft_deleted_and_missing_tasks` (skipped count); `::test_pull_handles_empty_or_header_only_sheet` (no exception on empty data) |
| NFR-CR-04-1  | `test_intent_pipeline.py` (stage-failure tests), `test_intent_graph.py` (per-node failure isolation), `test_owner_focused_prompt.py` (owner-stage failure) |
| NFR-CR-04-2  | `test_nfr_01_05.py` (`test_nfr2_dedup_retry_from_slack_does_not_post_new_card`)                              |
