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

Original FR-1..FR-12 from the base SPEC apply unchanged. CR-01 adds:

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
missing field (ordered: title → due_date → owner for tasks; title →
datetime_at → participants for meetings). The first question is prefixed
with `:memo: Записал: *<title>*.` so the user sees what was recorded.

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
- Calendar sync for meetings.
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
`no_action` but the prefilter hint is `create_task` / `create_meeting`,
`classify_with_backend` synthesises a minimal draft from the source
text (with the date phrase stripped) at the prefilter's confidence.
The prefilter is no longer used as a *gate*: the LLM pipeline runs on
every passive message when a backend is configured.

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
| NFR-CR-04-1  | `test_intent_pipeline.py` (stage-failure tests), `test_intent_graph.py` (per-node failure isolation), `test_owner_focused_prompt.py` (owner-stage failure) |
| NFR-CR-04-2  | `test_nfr_01_05.py` (`test_nfr2_dedup_retry_from_slack_does_not_post_new_card`)                              |
