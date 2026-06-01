# Database Schema — Humanoid CEO Brain / Task Manager

End-to-end map of the Postgres schema: how tasks, the team, counterparties,
meetings, ingestion and Google-Sheet sync are organised and linked.

**Conventions**
- Most tables mix in `TimestampMixin` → `created_at` / `updated_at`
  (timestamptz, server default `now()`). Omitted from diagrams for brevity.
- `PK` = primary key, `FK` = foreign key (hard DB constraint). Soft links
  (polymorphic id columns with no FK) are listed in *Cross-domain links*.
- Enums are stored as strings; their allowed values are shown in comments.
- The schema is owned by Alembic migrations `0001…0038` (`alembic/versions`).

The system has seven domains:

| # | Domain | Purpose |
|---|--------|---------|
| A | Intent → Task → Sync | the core: message → classified intent → draft → task → Google mirror |
| B | Directories | who/what tasks refer to: team, employees, counterparties |
| C | Meeting pipeline | Zoom/Fireflies recordings, agendas, counterparty briefs |
| D | Vector / entity matching | pgvector embeddings + the new dedup entity catalog |
| E | Ingestion plumbing | raw Slack/Telegram capture, dedup, CEO-brain responder |
| F | Google Sheet sync engine | versioned two-way task↔sheet sync (`gs_*`) + OAuth |
| G | Audit | append-only event log |

---

## A. Intent → Task → Sync (core)

A Slack/Telegram message is snapshotted with its surrounding context, the LLM
classifies an **intent**, which produces one or more **action drafts**; on
confirmation a draft materialises a **task** (or meeting). Tasks mirror 1:1 to
a Google Sheet row and a Google Tasks entry.

```mermaid
erDiagram
    CONTEXT_SNAPSHOTS ||--o{ INTENT_INFERENCES : "classified from"
    INTENT_INFERENCES ||--o{ ACTION_DRAFTS : "yields"
    ACTION_DRAFTS }o--o| TASKS : "materializes (task_id, SET NULL)"
    CONTEXT_SNAPSHOTS ||--o{ TASKS : "provenance"
    TASKS ||--o{ TASKS : "parent_task_id (subtasks)"
    TASKS ||--o{ TASK_STATUS_HISTORY : "transitions"
    TASKS ||--o{ TASK_SUBSCRIPTIONS : "watchers"
    TASKS ||--o{ DAILY_PLAN_ITEMS : "planned on a day"
    TASKS ||--o| GOOGLE_SHEETS_SYNC : "1:1 mirror"
    TASKS ||--o| GOOGLE_TASKS_SYNC : "1:1 mirror"
    CONTEXT_SNAPSHOTS ||--o{ MEETINGS : "provenance"

    CONTEXT_SNAPSHOTS {
        int id PK
        str conversation_id "Slack channel / TG chat id"
        str source_ts "triggering message timestamp"
        str thread_ts "thread root, if any"
        json source_message "the message that triggered extraction"
        json history_before "N messages above (Slack=10, TG ~10k chars)"
        json thread_messages "thread replies pulled for context"
    }
    INTENT_INFERENCES {
        int id PK
        int context_snapshot_id FK "context it was classified from"
        enum intent "create_task|create_meeting|update_*|no_action"
        float confidence "model confidence 0..1"
        str invocation_type "passive|mention|shortcut"
        json raw "raw LLM tool output"
        str reasoning "LLM rationale (audit)"
    }
    ACTION_DRAFTS {
        int id PK
        int inference_id FK "parent inference"
        enum intent "draft kind"
        enum state "proposed|confirmed|edited|ignored|expired|failed"
        json payload "task fields incl. direction + _pending provenance"
        str created_by_slack_user_id "author"
        int task_id FK "task created on confirm (SET NULL)"
        str awaiting_field "slot being asked back, if clarifying"
    }
    TASKS {
        int id PK
        str title "imperative short title"
        str description "details"
        str owner_user_id "resolved Slack/TG uid"
        str owner_display_name "clean human name (FR-232)"
        enum priority "low|medium|high|urgent"
        date due_date "deadline (always set; today fallback)"
        time due_time "optional time-of-day"
        enum status "backlog|todo|in_progress|done"
        str category "free-form bucket"
        bool is_recurring "recurring template flag"
        int parent_task_id FK "self-ref subtask parent"
        enum source_kind "slack|telegram|fireflies|zoom"
        str source_conversation_id "origin channel/chat"
        str source_message_ts "origin message ts"
        str source_permalink "link back to origin"
        int context_snapshot_id FK "provenance"
        str google_tasks_id "mirror id (denormalised)"
        json extra "direction + channel-specific metadata"
        datetime deleted_at "soft delete"
    }
    MEETINGS {
        int id PK
        str title
        json participants "names/handles"
        datetime datetime_at "scheduled time"
        str timezone
        enum status "scheduled|cancelled|done"
        int context_snapshot_id FK
    }
    TASK_STATUS_HISTORY {
        int id PK
        int task_id FK
        enum from_status
        enum to_status
        datetime changed_at
    }
    TASK_SUBSCRIPTIONS {
        int id PK
        int task_id FK
        str user_id "subscriber uid"
    }
    DAILY_PLAN_ITEMS {
        int id PK
        str user_id "whose plan"
        date plan_date "the day"
        int task_id FK
        datetime excluded_at "removed from that day"
    }
    GOOGLE_SHEETS_SYNC {
        int id PK
        int task_id FK "unique 1:1"
        str spreadsheet_id
        int row_id "sheet row"
        enum status "pending|synced|error"
    }
    GOOGLE_TASKS_SYNC {
        int id PK
        int task_id FK "unique 1:1"
        str tasklist_id
        str google_task_id
        enum status
    }
```

---

## B. Directories (people & organisations)

Three independent directories that tasks/meetings refer to. **`team_members`**
is the operator's curated team (synced from a Google Sheet, keyed for both
Telegram and Slack). **`employees`** is the Slack-workspace user cache.
**`counterparties`** is the external org/contact hub, with a JSON attribute
satellite and a mention log.

```mermaid
erDiagram
    COUNTERPARTIES ||--o{ COUNTERPARTY_ATTRS : "key/value satellite"
    COUNTERPARTIES ||--o{ COUNTERPARTY_MENTIONS : "seen in meetings/chats"

    TEAM_MEMBERS {
        int id PK
        str real_name "canonical display (source of truth)"
        int telegram_user_id "TG identity"
        str telegram_username "@handle"
        str slack_user_id "Slack identity"
        str role "job role"
        str email
        bool active "in current roster"
        datetime last_synced_at "from team Google Sheet"
    }
    EMPLOYEES {
        str slack_user_id PK "Slack workspace user id"
        str team_id "Slack team/workspace"
        str display_name "Slack display"
        str real_name "Slack real name"
        str email
        str title "Slack profile title"
        bool is_bot
        bool is_admin
        datetime last_seen_at "last observed in traffic"
        json profile_raw "cached Slack profile"
    }
    COUNTERPARTIES {
        int id PK
        str name "canonical org/contact name"
        str name_normalised "fuzzy-match dedup key (unique)"
    }
    COUNTERPARTY_ATTRS {
        int id PK
        int counterparty_id FK
        str source "where attrs came from (sheet/canonical/…)"
        json attributes "Type, Company, Contact Info, …"
        datetime captured_at
    }
    COUNTERPARTY_MENTIONS {
        int id PK
        int counterparty_id FK
        str source_kind "zoom|fireflies|slack|telegram"
        str source_id "recording/message id"
        str context "surrounding text (often NULL today)"
    }
```

---

## C. Meeting pipeline (Zoom / Fireflies → docs, briefs, prompts)

Recordings are downloaded, transcribed, summarised, exported to a Google Doc,
and mined for tasks. `meeting_agendas` posts a pre-meeting brief (pulling prior
Zoom recordings). `counterparty_briefs*` research external parties before a
meeting. `counterparty_prompts*` are the Telegram yes/no cards that confirm
new counterparties (auto-enrollment, now gated off by default).

```mermaid
erDiagram
    COUNTERPARTY_BRIEFS_EVENTS ||--o{ COUNTERPARTY_BRIEF_LINKS : "groups"
    COUNTERPARTY_BRIEFS ||--o{ COUNTERPARTY_BRIEF_LINKS : "linked into event"
    COUNTERPARTY_PROMPT_BATCHES ||--o{ COUNTERPARTY_PROMPTS : "batch of yes/no cards"

    ZOOM_RECORDINGS {
        int id PK
        str zoom_id "unique recording id"
        str title
        datetime meeting_date
        int duration_seconds
        json participants
        json calendar_attendees "matched from Google Calendar"
        str transcript_text
        str detailed_summary
        str short_summary
        str google_doc_url "exported report"
        bool tasks_extracted
        str slack_post_ts "summary post in Slack"
    }
    MEETING_RECORDINGS {
        int id PK
        str fireflies_id "unique recording id"
        str title
        datetime meeting_date
        json participants
        json calendar_attendees
        str transcript_text
        str detailed_summary
        str short_summary
        str google_doc_url
        bool tasks_extracted
    }
    MEETING_AGENDAS {
        int id PK
        str calendar_event_id "Google Calendar event"
        str title
        datetime scheduled_start_at
        datetime posted_at
        str slack_channel
        str google_doc_url
        json prior_meeting_zoom_ids "context recordings"
    }
    COUNTERPARTY_BRIEFS {
        int id PK
        str counterparty_key "normalised key"
        str kind "org|person"
        str display_name
        str org_name "person's org, if person"
        int counterparty_id "soft link to COUNTERPARTIES"
        json research_payload "LLM research output"
        decimal cost_usd
        str google_doc_url
    }
    COUNTERPARTY_BRIEFS_EVENTS {
        int id PK
        str calendar_event_id
        str event_title
        datetime scheduled_meeting_at
        str slack_channel
        decimal total_cost_usd
    }
    COUNTERPARTY_BRIEF_LINKS {
        int id PK
        int event_id FK
        int brief_id FK
    }
    COUNTERPARTY_PROMPTS {
        int id PK
        str source_kind
        str source_id
        str mention_text "raw mention"
        str mention_normalised
        int chat_id "TG chat"
        str status "pending|yes|no|…"
        int created_counterparty_id "soft link if confirmed"
        int batch_id FK
        bool selected
        str canonical_name_corrected
    }
    COUNTERPARTY_PROMPT_BATCHES {
        int id PK
        str source_kind
        str source_id
        int chat_id
        int user_id
    }
```

---

## D. Vector / entity matching

`entity_embeddings` is the pgvector satellite (one current embedding per
source entity, polymorphic `entity_id`). `entity_catalog_staging` /
`entity_catalog_ingest` are the new deduplicated entity catalog being built
from the operator's export (FR-CR-05-231). `entity_resolution_cache` memoises
LLM resolution results.

```mermaid
erDiagram
    ENTITY_CATALOG_INGEST ||..o{ ENTITY_CATALOG_STAGING : "chunks feed (provenance, no FK)"

    ENTITY_EMBEDDINGS {
        int id PK
        str kind "counterparty|team_member|employee"
        str entity_id "source row id (polymorphic, text)"
        str model "e.g. text-embedding-3-large"
        int dim "3072"
        vector embedding "pgvector(3072), exact cosine"
        str text_repr "exact text embedded (name+context)"
        str text_repr_hash "sha256 for staleness check"
    }
    ENTITY_CATALOG_STAGING {
        int id PK
        str name "canonical display name"
        str name_normalised "dedup key (unique with is_org)"
        bool is_org "true=organisation, false=person"
        str parent_org "person's affiliation"
        str description "LLM-assembled context (embedded w/ name)"
        str aliases "newline-joined surface variants"
        int mentions_count "merge counter across chunks"
    }
    ENTITY_CATALOG_INGEST {
        int chunk_no PK "deterministic chunk index"
        int row_lo "source logical row range start"
        int row_hi "range end"
        int char_len
        int entities_found
        str status "done|error (resume/idempotency)"
        str note
    }
    ENTITY_RESOLUTION_CACHE {
        str cache_key PK "sha256 of inputs"
        json payload "cached resolution result"
        datetime expires_at "TTL"
        int hits_count
    }
```

---

## E. Ingestion plumbing (raw capture + dedup)

Raw Slack/Telegram traffic and idempotency stores. `slack_conversations` /
`slack_messages` mirror channels the bot sees; `*_archive` keep raw events;
`processed_*` tables enforce exactly-once handling. `claude_responder_runs`
tracks the CEO-brain @-mention responder. Telegram has its own member +
listener-offset tables.

```mermaid
erDiagram
    SLACK_CONVERSATIONS ||--o{ SLACK_MESSAGES : "has"

    SLACK_CONVERSATIONS {
        str id PK "Slack channel id"
        str team_id
        str kind "im|mpim|channel|group"
        str name
    }
    SLACK_MESSAGES {
        int id PK
        str conversation_id FK
        str ts "message timestamp"
        str thread_ts
        str user_id
        str subtype
        str text
        str transcript "if audio message"
        str permalink
        json raw
    }
    SLACK_EVENTS_ARCHIVE {
        int id PK
        str event_id "Slack event id"
        str event_type
        str conversation_id
        str ts
    }
    PROCESSED_SLACK_EVENTS {
        str event_id PK "dedup: handle each event once"
        datetime received_at
    }
    SLACK_MESSAGE_ARCHIVE {
        int id PK
        str channel_id
        str ts
        str user_id
        str text
        date day "partition day"
        int edit_count
        datetime deleted_at
    }
    CLAUDE_RESPONDER_RUNS {
        int id PK
        str slack_channel_id
        str slack_event_ts
        str response_text
        json tool_uses
        str status "ok|error|…"
        str error
    }
    PROCESSED_TELEGRAM_MESSAGES {
        int chat_id PK
        int message_id PK
        int task_id FK "task created from it (SET NULL)"
        datetime processed_at
    }
    TELEGRAM_LISTENER_STATE {
        int id PK
        int last_update_id "long-poll offset"
        datetime updated_at
    }
    TELEGRAM_CHAT_MEMBERS {
        int chat_id PK
        int user_id PK
        str username
        str first_name
        str last_name
        bool has_started_bot
        datetime last_seen_at
    }
```

---

## F. Google Sheet sync engine (`gs_*`) + OAuth

Versioned, append-only two-way sync between tasks and a Google Sheet
(`app/sheet_sync`). An *integration* is one (spreadsheet, tab); *records* are
sheet rows identified by DeveloperMetadata; *record states* are content
versions; *sync runs* + *errors* + *snapshots* give an audit trail.
`gs_exported_sources` dedups which drafts/tasks were already appended.

```mermaid
erDiagram
    GS_SHEET_INTEGRATIONS ||--o{ GS_RECORDS : "rows"
    GS_RECORDS ||--o{ GS_RECORD_STATES : "content versions"
    GS_SHEET_INTEGRATIONS ||--o{ GS_SYNC_RUNS : "runs"
    GS_SYNC_RUNS ||--o{ GS_SYNC_ERRORS : "errors"
    GS_SHEET_INTEGRATIONS ||--o{ GS_SYNC_ERRORS : "scope"
    GS_SHEET_INTEGRATIONS ||--o{ GS_SHEET_SNAPSHOTS : "raw snapshots"
    GS_SYNC_RUNS ||--o{ GS_SHEET_SNAPSHOTS : "per run"
    GS_SHEET_INTEGRATIONS ||--o{ GS_TASK_ROW_MAPPINGS : "row-record map"
    GS_RECORDS ||--o{ GS_TASK_ROW_MAPPINGS : "maps to"
    GS_SHEET_INTEGRATIONS ||--o{ GS_TASK_CONFIGS : "column config"
    GS_SHEET_INTEGRATIONS ||--o{ GS_EXPORTED_SOURCES : "append dedup"

    GS_SHEET_INTEGRATIONS {
        str id PK "uuid"
        str entity_type "task"
        str spreadsheet_id
        int sheet_id "gid"
        str sheet_title "tab"
        str status "draft|active"
        int sync_interval_seconds
        str timezone
    }
    GS_RECORDS {
        str id PK "uuid"
        str integration_id FK
        str entity_type
        str business_key "stable row key (DeveloperMetadata)"
        str current_state_id "latest version"
        datetime deleted_at "soft delete"
    }
    GS_RECORD_STATES {
        str id PK "uuid"
        str record_id FK
        str payload_hash "content hash (idempotency)"
    }
    GS_SYNC_RUNS {
        str id PK
        str integration_id FK
    }
    GS_SYNC_ERRORS {
        str id PK
        str run_id FK
        str integration_id FK
    }
    GS_SHEET_SNAPSHOTS {
        str id PK
        str integration_id FK
        str run_id FK
    }
    GS_TASK_ROW_MAPPINGS {
        str id PK
        str integration_id FK
        str record_id FK
    }
    GS_TASK_CONFIGS {
        str id PK
        str integration_id FK
    }
    GS_EXPORTED_SOURCES {
        str id PK
        str integration_id FK
    }
    OAUTH_CREDENTIALS {
        int id PK
        str provider "google"
        str user_key "slack uid or email"
        str access_token_ciphertext "encrypted"
        str refresh_token_ciphertext "encrypted"
        str scopes
        datetime token_expires_at
    }
```

---

## G. Audit

```mermaid
erDiagram
    AUDIT_LOGS {
        int id PK
        str category "event|draft|sync|…"
        str action "what happened"
        str entity_type "task|draft|…"
        str entity_id
        str actor "who/what"
        json payload "details"
    }
```

---

## Cross-domain links (polymorphic / soft — no DB FK)

These connect domains without a hard foreign key (by design — satellites and
provenance must survive source-row churn):

- **`entity_embeddings.entity_id`** → `counterparties.id` / `team_members.id` /
  `employees.slack_user_id`, disambiguated by `kind`.
- **`counterparty_mentions.(source_kind, source_id)`** →
  `zoom_recordings` / `meeting_recordings` / `slack_messages` / Telegram.
- **`tasks.(source_kind, source_conversation_id, source_message_ts)`** →
  the originating Slack/Telegram message or Zoom/Fireflies recording.
- **`tasks.owner_user_id` / `action_drafts.created_by_slack_user_id`** →
  `team_members` / `employees` (resolved at extraction time).
- **`counterparty_briefs.counterparty_id`** and
  **`counterparty_prompts.created_counterparty_id`** → `counterparties.id`
  (nullable soft links).
- **`meeting_agendas.prior_meeting_zoom_ids`** (JSON) → `zoom_recordings.zoom_id`.
- **`tasks.context_snapshot_id` / `meetings.context_snapshot_id`** →
  `context_snapshots.id` (also the audit trail for *why* the task exists).

## Data flow in one line

`Slack/Telegram msg` **or** `Zoom/Fireflies transcript`
→ `context_snapshots` → `intent_inferences` → `action_drafts`
→ **`tasks`** → `google_sheets_sync` / `google_tasks_sync`
→ surfaced via Telegram cards & the strategic Slack digest; entities in the
text are resolved against the **directories** (B) using the **vector layer** (D).
