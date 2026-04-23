# Slack Task Manager Bot

Slack bot that listens to messages in DMs / MPIMs / channels, detects
`create_task` / `create_meeting` / `update_task` / `update_meeting` intents,
proposes a draft action for confirmation, persists confirmed entities in
Postgres, and syncs tasks to Google Sheets and Google Tasks.

Implements the SPEC in `spec/` (US-1 passive detection, US-2 explicit action,
US-3 persistence + sync), with source-message traceability on every entity.

## Product rules (hard invariants)

- **Source of truth:** the database.
- **Slack:** intake + confirm UI + feedback only.
- **Google Sheets:** operational read view.
- **Google Tasks:** execution surface.
- **Passive mode never auto-creates.** Explicit mode still requires confirm.
- Every entity keeps source Slack message + context snapshot reference.
- Context window = source message + last 10 messages + thread (configurable).
- Meetings in MVP are DB-only; Calendar sync is explicitly out of scope.

## Architecture

```
Slack event  ─▶  Event Gateway (Bolt)  ─▶  dedup ─▶  Context Retrieval
                                                      │
                                                      ▼
                                            Intent Classification
                                            (rules prefilter + LLM)
                                                      │
                                                      ▼
                                               Action Orchestrator
                                     ┌────────────┴────────────┐
                                 high conf.                  medium
                                  draft card                 soft prompt
                                     │                          │
                                     ▼                          ▼
                           user Confirm / Edit / Ignore   user Yes / No
                                     │
                                     ▼
                              Finalize Service
                         ┌──────────┼──────────────────┐
                    DB persist    Sheets sync        Google Tasks sync
                         │                                     │
                         └────────────────┬────────────────────┘
                                          ▼
                                    Slack feedback
```

Key modules:

| Path | Purpose |
| ---- | ------- |
| `app/slack_bot/app.py` | Bolt app + handler registration |
| `app/slack_bot/handlers/` | events, shortcuts, actions, views |
| `app/slack_bot/blocks.py` | Block Kit builders (cards + modals) |
| `app/slack_bot/dedup.py` | Slack `event_id` dedup store |
| `app/slack_bot/rate_limiter.py` | `chat.postMessage` rate-aware sender |
| `app/context/retriever.py` | `conversations.history` + `.replies` |
| `app/intent/rules.py` | Cheap rule-based prefilter |
| `app/intent/classifier.py` | Anthropic tool-use extraction |
| `app/orchestrator/service.py` | Confidence policy + draft persistence |
| `app/orchestrator/finalize.py` | Confirm → DB → sync → feedback |
| `app/persistence/tasks.py` / `meetings.py` | Create entities with source linkage |
| `app/sync/sheets.py` | Append/update task row |
| `app/sync/tasks_api.py` | Insert/patch Google Task |
| `app/sync/google_auth.py` | Fernet-encrypted OAuth token store |

## Data model

See `alembic/versions/0001_initial.py`:

- `slack_conversations`, `slack_messages`, `context_snapshots`
- `processed_slack_events` (dedup)
- `intent_inferences`, `action_drafts`
- `tasks`, `meetings`
- `google_sheets_sync`, `google_tasks_sync`
- `audit_logs`, `oauth_credentials`

## Setup

### 1. Slack app

Create a Slack app and enable:

- **Socket Mode** (generate an app-level token, scope `connections:write`)
- **Event Subscriptions** — subscribe to:
  - `app_mention`
  - `message.im`
  - `message.mpim`
  - `message.channels` (if used in public channels)
  - `message.groups` (if used in private channels)
- **Interactivity & Shortcuts** — create two message shortcuts with
  callback_ids `create_task_from_message` and `create_meeting_from_message`.
- Bot scopes: `chat:write`, `app_mentions:read`, `im:history`, `mpim:history`
  (+ `channels:history` / `groups:history` if channels are in scope).

Install the app into the workspace and invite the bot into target
conversations.

### 2. Environment

Copy `.env.example` to `.env` and fill in:

- `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`
- `DATABASE_URL` — Postgres DSN
- `ANTHROPIC_API_KEY` — optional but strongly recommended; without it the bot
  falls back to the rule prefilter only
- `SECRETS_ENCRYPTION_KEY` — generate with:
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`
- `GOOGLE_SHEETS_SPREADSHEET_ID`, `GOOGLE_TASKS_DEFAULT_TASKLIST_ID`

### 3. Install + migrate

```bash
pip install -e .[dev]
alembic upgrade head
```

### 4. Run

```bash
python -m app.main
```

The process connects to Slack via Socket Mode — no public HTTP endpoint
required.

## Confidence policy (NFR-4 / NFR-5)

Configured via `INTENT_CONFIDENCE_HIGH` and `INTENT_CONFIDENCE_LOW`:

| Bucket | Range | Passive UX |
| ------ | ----- | ---------- |
| high | ≥ `INTENT_CONFIDENCE_HIGH` | full draft card with Confirm / Edit / Ignore |
| medium | `[INTENT_CONFIDENCE_LOW, HIGH)` | soft prompt ("Похоже, это задача. Создать?") |
| low | < `INTENT_CONFIDENCE_LOW` | silent, log inference for audit |

`no_action` is always silent, regardless of confidence.

## Google OAuth

Sync services share a service-account-like OAuth record keyed `_service_account`
in `oauth_credentials`. Implement an admin OAuth flow at deploy time to seed
this record (scopes: `spreadsheets`, `tasks`). Tokens are encrypted at rest
with Fernet; the key comes from `SECRETS_ENCRYPTION_KEY`.

## Tests

```bash
pytest -q
```

24 tests cover rules, orchestrator confidence policy, Block Kit shape,
persistence with source linkage, context retrieval ordering, and event
deduplication. Tests use in-memory SQLite — no external services required.

## Operational notes

- Event ingestion acks immediately; heavy work runs inside `session_scope`
  blocks. Extend with a real queue (Redis/RQ) when scaling beyond a single
  process.
- `chat.postMessage` is throttled to 1 msg/s per channel with automatic
  `Retry-After` handling (Slack's standard rate limit).
- Slack `event_id` is stored on first sight; retries are silently ignored.
- Sync failures do **not** abort persistence; they are recorded on
  `google_sheets_sync` / `google_tasks_sync` for later retry.

## Not included in MVP (explicitly)

- Google Calendar sync for meetings
- HTTP mode / public URL deployment (Socket Mode only)
- Redis-based queue (in-process for MVP)
- Per-user Google OAuth UI (single service record for MVP)
