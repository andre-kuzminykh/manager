# Slack Task Manager — Product Spec (English)

**Version 1 · Product-facing, non-technical**
For a developer-facing technical reference see `SPEC.md`.

---

## 1. What this agent is

A Slack-native AI task manager. The bot lives inside Slack, listens to
messages, and turns conversations into tracked tasks without anyone
leaving the chat. Tasks land in a shared Google Sheet in real time,
and people get plans and reminders pushed into their Slack DMs.

The product covers the full task lifecycle: capture → triage →
schedule → execute → report. No separate UI, no second tool to learn.

## 2. Why it exists

Most teams already coordinate work in Slack threads, voice notes, and
DMs. Tasks discussed there get forgotten because moving them into
Jira / Notion / Asana is friction nobody pays. This bot removes the
friction:

- It **captures the task in place** (mention, shortcut, voice, or
  passive detection of "we need to do X by Friday").
- It **tracks the lifecycle in place** (buttons on the Slack card —
  Start, Mark done, Cancel, Edit, Delete).
- It **reports the state in place** (DMs for digests + reminders, plus
  a live-synced Google Sheet for managers).

Net result: the team works where it already is; managers get
visibility through the sheet and DM digests they don't have to chase.

## 3. User roles

The bot serves four roles. Most people fill several at once.

| Role | What's in it for them |
|---|---|
| **Contributor** (anyone in a Slack workspace) | Captures tasks from chat in two clicks; never has to type things into another tool. Gets a daily plan in DM, gets reminders before deadlines slip. |
| **Task owner** (the assignee of a specific task) | Sees clean cards with Start/Mark done/Cancel/Edit. Decides when to start, when to drop, what to attach as a result. Doesn't get pinged by digests for tasks that aren't theirs. |
| **Subscriber** (anyone watching someone else's task) | Sees the task in their *Tracking* section, gets DM updates when the status changes — without owning the work. Useful for stakeholders, dependent reviewers, leadership. |
| **Admin / manager** | Sees the whole team's open work in one DM each morning + evening. Can edit, reassign, or delete any task. Sees a real-time Google Sheet with every task's current state and history. |

There's also one off-stage role:

| Role | What's in it for them |
|---|---|
| **Operator** (the person who runs the bot) | Configures Slack tokens, OpenAI/Anthropic key, Google Service Account; the bot then runs unattended in Docker on a single VM. |

---

## 4. Features

Each feature lists the user stories it serves and a step-by-step flow
for each story. "Flows" describe the user-visible sequence; the bot's
internal mechanics are deliberately omitted.

### Feature 1 — Task capture

The bot supports four entry points so people can capture a task in
whatever way fits the moment.

#### 1.1 Capture via @-mention

> **As a contributor**, I want to mention the bot in any channel and
> describe a task, **so that** the task is captured without me opening
> another tool.

**Flow:**
1. User writes `@bot нужно к завтра подготовить презу` in a channel.
2. The bot reads the message, extracts title + due date + assignee
   from the text.
3. The bot replies in the same thread with a **draft card** showing
   the extracted fields and three buttons: *Accept*, *Edit*, *Reject*.
4. User clicks *Accept*. The card morphs into a confirmed task card.
5. The task is now in the system; a row appears in the Google Sheet.

#### 1.2 Capture via message shortcut

> **As a contributor**, I want to right-click a colleague's message
> and turn it into a task, **so that** I don't have to retype what
> they already wrote.

**Flow:**
1. User right-clicks any Slack message → *More actions* → *Create task
   from message*.
2. A modal opens, prefilled with the message text as the task title
   and the message author as a possible owner.
3. User adjusts owner / due date / priority and clicks *Submit*.
4. Confirmed task card is posted in the source thread; row appears in
   the sheet.

#### 1.3 Capture from a voice note

> **As a contributor on the go**, I want to record a Slack voice
> message describing a task, **so that** the bot transcribes it and
> creates the task without me typing.

**Flow:**
1. User records a voice note in Slack.
2. Bot downloads the audio, transcribes it (OpenAI Whisper), and
   feeds the transcript into the same extraction pipeline as text
   messages.
3. Bot replies with the same draft card as in flow 1.1.
4. User Accepts / Edits / Rejects.

#### 1.4 Passive capture (the bot notices for you)

> **As a contributor in a busy channel**, I want the bot to notice
> when someone describes a task ("we need X by Friday"), **so that**
> I don't have to remember to capture it manually.

**Flow:**
1. Someone writes in a channel where the bot is present: *"надо
   подготовить отчёт к понедельнику"*.
2. Bot reads the message, decides "this looks like a task", and posts
   a draft card with the extracted fields plus the three buttons.
3. Whoever cares (often the implied owner or the message author)
   clicks *Accept*, *Edit*, or *Reject*.
4. If nobody reacts in some time, the draft simply expires — nothing
   is created without a human click.

---

### Feature 2 — Task lifecycle

Once a task is confirmed, every status transition is one click on the
card. The lifecycle is deliberately simple: `backlog → todo →
in_progress → done`. There are also two cross-cutting actions —
*Cancel* (un-schedule) and *Delete* (remove).

#### 2.1 Start working

> **As the task owner**, I want to mark a task as started, **so that**
> my team sees what I'm actively working on.

**Flow:**
1. Owner sees the task card (in the source channel or in their
   morning plan DM).
2. Owner clicks *Start*.
3. Card updates: status flips to `in_progress`, *Start* button
   replaced by *Mark done*, started timestamp recorded.
4. All subscribers of the task get a DM notification.
5. The Google Sheet row is updated.

#### 2.2 Complete a task

> **As the task owner**, I want to close a task and optionally
> attach the result, **so that** the team has evidence of what was
> done.

**Flow:**
1. Owner clicks *Mark done* on the task card.
2. A small modal opens with two optional fields: *Artifact URL* and
   *Or description*.
3. Owner can either fill one of them and click *Complete*, or leave
   both empty and click *Complete* anyway.
4. Status flips to `done`. Card buttons collapse to read-only state.
5. Subscribers get a DM. Sheet row reflects `done` + the artifact.

#### 2.3 Cancel — "I won't get to it now"

> **As the task owner**, I want to drop a task back into the queue
> without closing it, **so that** I can come back to it later without
> the manager thinking I've abandoned it.

**Flow:**
1. Owner clicks *Cancel* on the task card.
2. Bot decides where the task goes:
   - if its `due_date` is within this week (Mon–Sun) → back to `todo`;
   - otherwise → back to `backlog`.
3. Card refreshes with the new status.
4. Subscribers get a DM about the status change.
5. Sheet row is updated, with `reason="cancelled"` recorded in the
   task's status history.

#### 2.4 Delete — "this shouldn't exist"

> **As the task owner or admin**, I want to remove a task entirely
> with confirmation, **so that** I can clean up duplicates or
> mistakes without losing the audit trail.

**Flow:**
1. Owner / admin clicks *Delete* on the task card.
2. A confirmation modal appears: *"This will delete task #N — title.
   The task is hidden from the UI immediately; the row stays in the
   audit log."*
3. User clicks *Delete* in the modal (or *Cancel* to back out).
4. Card is replaced by a tombstone: *":wastebasket: Task #N — title
   deleted by @actor"*.
5. Task disappears from every digest / plan / list / Google Sheet
   (the sheet row's status flips to `deleted` and the timestamp is
   filled).

---

### Feature 3 — Edit any field

> **As the task owner or admin**, I want to edit any field of an
> existing task, **so that** I can correct a wrongly-extracted owner,
> change priority, or update the deadline.

**Flow:**
1. Owner / admin clicks *Edit* on the task card.
2. A modal opens prefilled with all current fields: title,
   description, owner, priority, due date + time, start date + time,
   category, recurring schedule.
3. **Owner dropdown** lists every person the bot has seen in the
   workspace (real names from Slack profiles, never raw usernames).
4. User adjusts fields and clicks *Submit*.
5. Card refreshes with the new values; sheet row updates.

---

### Feature 4 — Subscriptions

Subscribers track work without owning it.

#### 4.1 Subscribe to someone else's task

> **As a stakeholder**, I want to subscribe to a colleague's task,
> **so that** I'm notified when its status changes — without owning
> the work.

**Flow:**
1. User opens any task card where they're not the owner.
2. User clicks *Subscribe*.
3. Bot DMs the user a confirmation with a copy of the task card.
4. From then on, every status change of that task posts a follow-up
   DM in the same thread (one task = one DM thread, so notifications
   don't fragment).
5. The button toggles to *Unsubscribe* on the card.

#### 4.2 Manage all subscriptions in one place

> **As a busy subscriber**, I want a single screen to see and prune
> what I'm subscribed to, **so that** I can clean up old interests
> in one go.

**Flow:**
1. From the daily digest DM, user clicks *Manage subscriptions*.
2. A modal opens listing every task they subscribe to.
3. Each row has an *Unsubscribe* button.
4. Click → that task drops out of the list immediately.

---

### Feature 5 — Daily plan

The daily plan is a two-step nudge: evening triage, morning execution.

#### 5.1 Evening — review tomorrow's plan

> **As a contributor**, I want a quick review of what's on my plate
> tomorrow, **so that** I can drop unrealistic items before bedtime.

**Flow:**
1. At 18:00 local time, bot DMs the user a card titled *"Plan for
   <tomorrow's date>"*.
2. The card lists each candidate task with a *Skip* button on the
   right.
3. At the bottom: *Approve plan* button (optional) and a *Tracking*
   section listing tasks the user subscribes to.
4. User clicks *Skip* on each task they won't get to. Each click drops
   that one task from tomorrow's plan.
5. User can click *Approve plan* to confirm explicitly — but it's
   **optional**. The morning plan runs whether they did or not.

#### 5.2 Morning — execute today's plan

> **As a contributor**, I want my plan ready when I open Slack in the
> morning, **so that** I can start working immediately.

**Flow:**
1. At 09:00 local time, bot DMs the user a *Today* card.
2. Each surviving task from yesterday's plan appears as a full task
   card with a *Start* button.
3. *Tracking* section shows tasks they subscribe to.
4. If the user **didn't** click *Approve plan* the night before, the
   DM gets a small note at the top: *":memo: Plan wasn't explicitly
   approved last night — running as-is."* — so the user knows what
   they're looking at.
5. User clicks *Start* on whatever they begin with.

---

### Feature 6 — Reminders & digests

Five separate reminder channels. Each runs on its own schedule and
is idempotent (one notification per (user, day), no spam on retry).

#### 6.1 Morning digest (per subscriber)

> **As a contributor**, I want a morning summary of what I own and
> watch, **so that** I know what to focus on today.

**Flow:**
1. Each morning, bot DMs every subscriber a digest with three
   sections: *Today*, *Approaching deadlines (next 2 days)*,
   *Overdue*.

#### 6.2 Weekly plan (Sunday)

> **As a contributor**, I want a heads-up on Sunday evening about
> the week ahead, **so that** I plan my week before Monday morning.

**Flow:**
1. Sunday 20:00, bot DMs every owner of `backlog` tasks due in the
   coming week with the list.

#### 6.3 Thread reminders (10:00 weekdays)

> **As a contributor**, I want a soft public nudge in the source
> thread of every active task, **so that** stakeholders see the task
> isn't forgotten and I'm reminded to update it.

**Flow:**
1. Weekday 10:00, bot posts a one-line reminder in the source thread
   of each task in `in_progress` (and `todo` tasks due this week).
2. The text tags the assignee: *":raised_hand: <@owner> task *#N* —
   what's the progress?"*.
3. One ping per (task, day), so the thread doesn't get spammed if
   the cron retries.

#### 6.4 Deadline reminders

> **As an owner**, I want a DM 2 days before the deadline and on
> the day of overdue, **so that** I don't miss things.

**Flow:**
1. Each morning, bot DMs the owner of every task whose due date is
   ≤ 2 days away or already overdue.
2. Includes the task title, due date, status.

#### 6.5 Admin watch-list

> **As an admin**, I want a daily snapshot of the team's active and
> stale work, **so that** I can prevent things from rotting.

**Flow:**
1. Mornings, every admin gets a DM with three sections: *In progress*
   (across the whole team), *Overdue* (across the team).
2. Evenings, admins get *Tomorrow* (tasks due tomorrow by assignee)
   plus *Stale* (`in_progress` with no movement for 2+ days).

---

### Feature 7 — Owner detection (no manual setup)

> **As a contributor**, I want the bot to figure out who a task is for
> based on the message text, **so that** I don't have to fill the
> assignee field manually for every task.

**Flow:**
1. When a task is being captured, the bot's LLM stage looks at the
   message + thread context.
2. If the message names someone explicitly ("Иван, сделай X" / "for
   @ivan" / "<@U123>"), bot picks that person — validated against the
   employees the bot knows about.
3. If nobody is named, bot quietly assigns the task to the message
   author (with an "(implicit)" badge on the card).
4. If the bot finds a name it doesn't recognise (e.g. "make Petya do
   it" but Petya isn't in the workspace), it asks in the source
   thread: *"I couldn't find Petya in the list. Who's the assignee?"*

---

### Feature 8 — Employees directory (auto-discovery)

> **As an operator**, I want the bot to learn the team automatically
> instead of me maintaining a list, **so that** new joiners are
> picked up without a redeploy.

**Flow:**
1. On bot startup, it walks the entire Slack workspace
   (`users.list`) and indexes every member.
2. When the bot is added to a channel / DM it doesn't know yet, it
   walks `conversations.members` and indexes everyone in that room.
3. When someone new posts a message, the bot upserts their profile
   from `users.info`.
4. The Edit-modal owner dropdown reads from this directory in real
   time. Names shown use Slack's `real_name` first (then
   `display_name`, then user id) so cells / dropdowns never read
   "admin" instead of the actual person.

---

### Feature 9 — Live Google Sheets sync

> **As a manager**, I want every task and its current state in a
> shared Google Sheet I can filter / pivot / chart, **so that** I
> have one place to see all work without opening Slack.

**Flow:**
1. Operator gives the bot a Google Service Account JSON and shares
   the target spreadsheet with the SA email.
2. Whenever a task is created, edited, started, completed, cancelled,
   or deleted, the bot **updates the same row** in the sheet within
   seconds.
3. Header row is written automatically on first sync — 21 columns
   in the bot's canonical order: id, title, description, owner,
   priority, category, start date/time, due date/time, recurring
   schedule, status, parent task id, source link, created/updated/
   deleted timestamps, completion artifact.
4. Deleted tasks stay in the sheet with status `deleted` for audit.

---

### Feature 10 — Audit & history

> **As an admin or auditor**, I want a complete record of who did
> what and when, **so that** I can investigate any task's life from
> creation to closure.

**Flow:**
1. Every status transition is recorded with `from`, `to`, `actor`,
   `reason`, `at` timestamps.
2. Every significant action (create, edit, delete, send digest,
   subscription change) is logged in `audit_logs`.
3. Every Slack message the bot sees is archived (text + transcript
   for voice).
4. Soft-deleted tasks are hidden from the UI but their history
   remains, including the `task_deleted` audit row with the actor
   and the status at the moment of deletion.

---

### Feature 11 — Recurring tasks

> **As an owner**, I want to mark a task as recurring on certain
> weekdays at certain times, **so that** ritual work (standups,
> reviews, etc.) doesn't need re-creating each week.

**Flow:**
1. In the Edit modal, user selects weekdays (Mon, Wed, Fri…) in a
   multi-select.
2. Optionally sets a time range (e.g. 09:00–11:30).
3. Submits. The card now shows a `:repeat: Mon/Wed/Fri 09:00–11:30`
   line.
4. Selecting weekdays IS the toggle — there's no separate "Recurring"
   checkbox. Empty selection = not recurring.

---

### Feature 12 — Categories & subtasks

> **As an owner**, I want to tag tasks by direction (e.g. marketing,
> engineering, ops) and break large tasks into subtasks, **so that**
> the sheet is filterable and the work hierarchy is explicit.

**Flow:**
1. *Category* is a free-text field in the Edit modal — write whatever
   makes sense (e.g. "marketing"). Stored as-is, used for filtering
   in the sheet.
2. *Subtasks* live in the data model as a self-reference (parent task
   id). For now they appear as separate tasks in the sheet with a
   `parent_task_id` column you can filter / group by. (No nested UI
   in Slack yet — see roadmap.)

---

## 5. What the bot does NOT do (intentional out-of-scope)

| Not done | Why |
|---|---|
| Calendar events / meetings | Used to. Removed by product decision — bot is task-only now. |
| Jira / Linear / Asana integration | The bot's own DB + Google Sheet are the single source of truth; no two-way sync needed. |
| Per-user timezones for reminders | All reminders run on the bot's server timezone (London). Per-user settings on the roadmap. |
| Multi-person DMs (group chats) | Bot doesn't request the `mpim:read` scope yet; per-channel sync skips MPIMs silently. Easy add — needs the scope and a re-install. |
| Nested subtask UI in Slack | Subtask data model exists; the dedicated UI is on the roadmap. |

---

## 6. Maturity

- Runs 24/7 in production on a single GCE VM in Docker.
- ~1055 automated tests, ~70 functional requirements documented in
  `SPEC.md` (technical companion to this doc).
- All migrations versioned with Alembic; safe rollback path on every
  schema change.
- All secrets (Slack tokens, OpenAI key, Google Service Account JSON)
  live outside the code in an env file + a mounted secrets directory.

---

## 7. Roadmap (not built, ranked by easy → hard)

1. **`mpim:read` Slack scope** — enable per-channel sync in group
   DMs (~1h work, just a re-install).
2. **Per-user timezones** — pull each user's Slack timezone and
   schedule their digest / plan in that zone.
3. **Subtask UI** — render subtasks under their parent on the card,
   and let owners create subtasks from a parent's modal.
4. **Sheet-side filters / pivots template** — ship a saved view in
   the spreadsheet so managers see "by owner / by category / overdue"
   without setting it up themselves.
5. **Per-project / per-channel scoping** — currently every channel
   feeds the same task list; teams might want isolated workspaces.
6. **External tracker mirror** (Jira / Linear) — one-way sync, lower
   priority because the sheet covers most reporting needs.
7. **Calendar / meetings** — re-enable the meeting modal if/when
   product decides it's back in scope.
