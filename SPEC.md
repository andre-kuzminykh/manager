# Slack Task Manager — Specification

Version: 3 (CR-01 + CR-02 merged)

This document consolidates:

- **Base SPEC** — initial product spec (passive detection, explicit mention,
  message shortcuts, DB persistence, Google Sheets + Google Tasks sync).
- **CR-01** — richer task lifecycle (Backlog → To Do → In Progress → Review →
  Done), explicit owner picker from an allowed list, workload-aware deadline
  proposal, subscribe/unsubscribe, start-work button, subscriber broadcasts,
  and daily / weekly / deadline digests.
- **CR-02** — conversational UX: @mention always produces a draft widget
  (fallback when the LLM is unsure), bot asks missing fields in the thread,
  user replies update the card in place via `chat.update`, widget disappears
  after Confirm/Ignore, Open-source button removed from task cards, daily
  digest gains a Tracking section and a Manage-subscriptions modal.

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
