# Slack Task Manager — Specification

Version: 2 (CR-01 merged)

This document consolidates:

- **Base SPEC** — initial product spec (passive detection, explicit mention,
  message shortcuts, DB persistence, Google Sheets + Google Tasks sync).
- **CR-01** — Change Request adding richer task lifecycle (Backlog → To Do →
  In Progress → Review → Done), explicit owner picker from an allowed list,
  workload-aware deadline proposal, subscribe/unsubscribe, start-work button,
  subscriber broadcasts, and daily / weekly / deadline digests.

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
Подписаться / Отписаться · Open source · Show context**

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
