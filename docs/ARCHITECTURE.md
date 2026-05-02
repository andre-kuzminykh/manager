# Architecture diagrams

Mermaid renderings of the manager bot's architecture. Each section
covers one layer; combined they document the live system. Update
when you add a pipeline step / model / integration / system prompt.

> Diagrams use [Mermaid](https://mermaid.js.org). Render with
> `docs/diagrams/` tooling or any markdown viewer with mermaid
> support (GitHub natively renders these).


## 1. Code layout

How the codebase is organised on disk.

```mermaid
graph TD
    APP[app/]:::pkg
    OPS[ops/]:::pkg
    APP --> CONFIG[config.py<br/>Settings + env vars]:::file
    APP --> DB[db.py<br/>session_scope + engine]:::file
    APP --> LOG[logging_setup.py<br/>structlog]:::file

    APP --> MODELS[models/<br/>SQLAlchemy ORM]:::pkg
    APP --> FIREFLIES[fireflies/<br/>pipeline + client + prompts]:::pkg
    APP --> ZOOM[zoom/<br/>pipeline + client]:::pkg
    APP --> SERVICES[services/<br/>shared business logic]:::pkg
    APP --> SYNC[sync/<br/>counterparties / team sheets / tasks]:::pkg
    APP --> TGBOT[telegram_bot/<br/>listener + sender + cards + handlers]:::pkg
    APP --> TGINGEST[telegram_ingest/<br/>messages → ActionDrafts]:::pkg
    APP --> ORCH[orchestrator/<br/>persist inference / drafts]:::pkg
    APP --> PERS[persistence/<br/>Task creation from draft]:::pkg
    APP --> INTENT[intent/<br/>LLM backends + classifier]:::pkg

    OPS --> CLI1[migrate_fireflies.py]:::file
    OPS --> CLI2[migrate_zoom.py]:::file
    OPS --> CLI3[pull_counterparties.py]:::file
    OPS --> CLI4[telegram_listener.py<br/>main process]:::file

    classDef pkg fill:#1e3a5f,stroke:#2d5a8b,color:#fff
    classDef file fill:#2a4a4a,stroke:#3a6a6a,color:#fff
```


## 2. Infrastructure

One bot container + external Postgres + external Google / OpenAI /
Telegram / Fireflies / Zoom APIs. Files mounted via host volumes.

```mermaid
flowchart LR
    subgraph host[VM host human-1]
        direction LR
        subgraph compose[docker-compose project: manager]
            MIGRATE[manager-migrate-1<br/>alembic upgrade head]
            BOT[manager-bot-1<br/>python -m ops.telegram_listener]
        end
        subgraph other[external docker network slack-task-net]
            DB[(slack-task-db<br/>postgres 16)]
            PROXY[pg-proxy<br/>:5432 → DB]
        end
        VOL_S[~/manager/secrets/<br/>sa.json]
        VOL_A[~/manager/audio/<br/>chunked mp3/m4a]
        VOL_T[~/manager/traces/<br/>per-recording JSONL]
    end

    BOT -- env_file --> ENV[.env<br/>OPENAI_API_KEY, FIREFLIES_*,<br/>ZOOM_*, GOOGLE_*, TELEGRAM_*]
    BOT --> VOL_S
    BOT --> VOL_A
    BOT --> VOL_T
    MIGRATE --> DB
    BOT --> DB

    BOT -.HTTPS.-> OAI[OpenAI API<br/>Whisper + Chat Completions]
    BOT -.HTTPS.-> FF[Fireflies GraphQL]
    BOT -.HTTPS.-> ZM[Zoom REST + OAuth]
    BOT -.HTTPS.-> TG[Telegram Bot API]
    BOT -.HTTPS.-> GS[Google Sheets API]
    BOT -.HTTPS.-> GD[Google Docs API]
    BOT -.HTTPS.-> GT[Google Tasks API]
    BOT -.HTTPS.-> SUPA[Supabase<br/>TG-source view]
```


## 3. Data layer (ER)

Core SQLAlchemy models and their key relationships.

```mermaid
erDiagram
    Task ||--o{ TaskStatusHistory : has
    Task ||--o| ContextSnapshot : "from"
    ActionDraft }o--|| IntentInference : "from"
    ActionDraft ||--o| Task : "materialises"
    IntentInference }o--|| ContextSnapshot : "on"
    Counterparty ||--o{ CounterpartyAttribute : "satellites (CASCADE)"
    Counterparty ||--o{ CounterpartyMention : "mentioned in"

    Task {
        int id PK
        string title
        string description
        string owner_user_id
        string owner_display_name
        enum priority
        date due_date
        time due_time
        enum status
        enum source_kind "telegram | fireflies | zoom | slack"
        string source_conversation_id
        string source_message_ts
        string source_permalink
        int context_snapshot_id FK
        timestamp deleted_at
    }

    TaskStatusHistory {
        int id PK
        int task_id FK
        enum from_status
        enum to_status
        string reason
        string changed_by_slack_user_id
        timestamp at
    }

    ActionDraft {
        int id PK
        int inference_id FK
        enum intent
        enum state "proposed | confirmed | edited | ignored | expired"
        json payload "title, description, owner, due, _pending"
        int task_id FK "nullable until confirm"
        string created_by_slack_user_id
    }

    IntentInference {
        int id PK
        int context_snapshot_id FK
        enum intent
        float confidence
        string invocation_type
        json raw "raw LLM tasks list"
        string reasoning
    }

    ContextSnapshot {
        int id PK
        string conversation_id
        string source_ts
        json source_message
        json history_before
        json thread_messages
    }

    MeetingRecording {
        int id PK
        string fireflies_id UK
        string title
        timestamp meeting_date
        int duration_seconds
        json participants
        string audio_path
        bool audio_downloaded
        bool transcribed
        text transcript_text
        bool detailed_summarised
        text detailed_summary
        text short_summary
        bool short_summary_sent
        string google_doc_id
        string google_doc_url
        bool tasks_extracted
        int tasks_extracted_count
    }

    ZoomRecording {
        int id PK
        string zoom_id UK
        string zoom_meeting_id
        string title
        timestamp meeting_date
        json participants "Zoom API metadata - fallback only"
        string audio_path
        text transcript_text
        text detailed_summary
        text short_summary
        string google_doc_url
    }

    Counterparty {
        int id PK
        string name
        string name_normalised UK "fold + translit + strip parens (FR-CR-05-132)"
    }

    CounterpartyAttribute {
        int id PK
        int counterparty_id FK
        string source "tab name"
        json attributes "JSONB row payload"
        timestamp captured_at
    }

    CounterpartyMention {
        int id PK
        int counterparty_id FK
        string source_kind "fireflies | zoom"
        string source_id "= meeting recording id"
        text context
    }

    TeamMember {
        int id PK
        string real_name
        int telegram_user_id
        string telegram_username
        string slack_user_id
        string role
        string notes
        bool active
    }
```


## 4. Service layer & data flow

Per-pipeline call graph: which step calls which service, what data
moves in. Both Fireflies and Zoom pipelines share the same shape;
the diagram below documents it once. Step numbers match the order
in `process_one`.

```mermaid
flowchart TD
    REC[(MeetingRecording /<br/>ZoomRecording row)]:::data

    S1[1 _step_download_audio]:::step
    S2[2 _step_transcribe]:::step
    S3[3 _step_detailed_summary]:::step
    S4[4 _step_match_counterparties]:::step
    S5[5 _step_extract_tasks]:::step
    S6[6 _step_verify_tasks]:::step
    S7[7 _step_canonicalize_task_names]:::step
    S8[8 _dedupe_meeting_tasks]:::step
    S9[9 _step_doc_export]:::step
    S10[10 _step_short_summary]:::step
    S11[11 _step_send_short_summary]:::step
    S12[12 _step_post_task_cards]:::step

    REC --> S1
    S1 --> AUDIO[/audio/recording_id.m4a/]:::file
    AUDIO --> S2
    S2 -- POST /audio/transcriptions --> WHISPER[OpenAI Whisper<br/>+ bias prompt]:::api
    WHISPER --> TR[transcript_text]:::data
    TR --> REC

    S2 -. read .-> BIAS[build_whisper_bias_prompt<br/>team_members + counterparties]:::svc

    TR --> S3
    S3 -- POST /chat/completions --> LLM[OpenAI gpt-5.5<br/>DETAILED_SUMMARY_SYSTEM]:::api
    LLM --> DS[detailed_summary]:::data
    DS --> REC

    DS --> S4
    TR --> S4
    DIR[(counterparties)]:::data --> S4
    S4 --> P1[extract_counterparty_mentions<br/>Pass 1 LLM JSON-mode]:::svc
    P1 --> P2[resolve_mentions_to_directory<br/>Pass 2 LLM JSON-mode]:::svc
    P2 --> CMAP[mention → canonical map]:::data
    P2 --> CM[(CounterpartyMention rows<br/>upsert by name_normalised)]:::data

    DS --> S5
    TR --> S5
    TM[(team_members)]:::data --> S5
    S5 -- LLM JSON-mode --> TASKEXT[TASK_EXTRACTION_SYSTEM]:::api
    TASKEXT --> S5
    S5 --> TASKS[(Tasks rows<br/>source_kind = fireflies/zoom)]:::data
    S5 --> WIPE[wipe prior tasks<br/>idempotent rerun]:::svc

    TASKS --> S6
    S6 -- LLM JSON-mode --> VFY[TASK_VERIFICATION_SYSTEM]:::api
    VFY --> S6
    S6 --> TASKS

    TASKS --> S7
    DIR --> S7
    CMAP --> S7
    S7 -- LLM JSON-mode Pass 3 --> CANON[CANONICALIZE_TASKS_SYSTEM]:::api
    CANON --> S7
    S7 --> TASKS_REWRITTEN[Tasks with canonical names]:::data

    TASKS_REWRITTEN --> S8
    S8 --> TASKS_DEDUPED[Tasks deduped<br/>topic-prefix + desc ratio ≥ 0.55]:::data

    DS --> S9
    TASKS_DEDUPED --> S9
    S9 -- Drive + Docs API --> GDOC[Google Doc URL]:::api
    GDOC --> REC

    DS --> S10
    TASKS_DEDUPED --> S10
    TM --> S10P[extract_zoom_participants_via_llm<br/>Zoom only]:::svc
    TR --> S10P
    S10P -- LLM JSON-mode --> PART[PARTICIPANTS_EXTRACT_SYSTEM]:::api
    PART --> S10P
    S10 -- LLM --> SHORT[SHORT_SUMMARY_SYSTEM]:::api
    S10 --> SS[short_summary text<br/>+ <a href> wrap to Doc]:::data
    SS --> REC

    SS --> S11
    S11 -- sendMessage --> TG[Telegram Bot API]:::api

    TASKS_DEDUPED --> S12
    S12 -- post_initial_card --> TG

    classDef step fill:#1f4068,stroke:#2d5a8b,color:#fff
    classDef svc fill:#2d4a4a,stroke:#3d6a6a,color:#fff
    classDef api fill:#5a3a1f,stroke:#7a5230,color:#fff
    classDef data fill:#1f4f2d,stroke:#2d6a40,color:#fff
    classDef file fill:#404040,stroke:#606060,color:#fff
```

### Listener auto-poll loops (background process)

```mermaid
flowchart LR
    LIST[telegram_bot/listener.py<br/>tick every 1-5 sec]:::step

    LIST --> M1[_maybe_poll_fireflies<br/>every 60s]:::svc
    LIST --> M2[_maybe_poll_zoom<br/>every 60s]:::svc
    LIST --> M3[_maybe_run_counterparties_pull<br/>every 300s]:::svc
    LIST --> M4[_maybe_run_team_sheet_pull<br/>every 60s]:::svc
    LIST --> M5[_maybe_run_tasks_sheet_pull<br/>every 60s]:::svc
    LIST --> M6[_maybe_pull_google_tasks<br/>every 60s]:::svc
    LIST --> M7[TG getUpdates long-poll<br/>30s]:::svc
    LIST --> M8[_maybe_poll_source_view<br/>Supabase TG ingest]:::svc

    M1 --> FFP[FirefliesPipeline.process_one]:::step
    M2 --> ZP[ZoomPipeline.process_one]:::step
    M3 --> SYNCC[CounterpartiesSheetSync.pull]:::svc
    M4 --> SYNCT[TeamSheetSync.pull]:::svc
    M5 --> SYNCS[TaskSheetSync.pull]:::svc
    M7 --> CB[handle_callback_query]:::svc

    classDef step fill:#1f4068,stroke:#2d5a8b,color:#fff
    classDef svc fill:#2d4a4a,stroke:#3d6a6a,color:#fff
```


## 5. Prompts & context

Every system-prompt + the data window each one sees. Prompts are in
`app/fireflies/prompts.py` (4) + `app/services/counterparty_match.py`
(3) + `app/services/zoom_participants.py` (1) = 8 total.

```mermaid
flowchart LR
    TR[transcript_text]:::data
    DS[detailed_summary]:::data
    KE[known_employees<br/>team_members table]:::data
    DIR[counterparties directory]:::data
    EXISTING[existing tasks<br/>this meeting]:::data
    TASKS_EXTRACTED[extracted tasks list]:::data
    EXTRACTED_MENTIONS[Pass-1 mentions list]:::data

    subgraph P_DETAILED[DETAILED_SUMMARY_SYSTEM]
        direction TB
        P_DETAILED_IN["transcript +<br/>meta"]
        P_DETAILED_OUT["6000-15000 char<br/>structured RU body"]
    end
    TR --> P_DETAILED_IN
    P_DETAILED_OUT --> DS

    subgraph P_SHORT[SHORT_SUMMARY_SYSTEM]
        direction TB
        P_SHORT_IN["meta + detailed_summary"]
        P_SHORT_OUT["≤2000 char Telegram<br/>DD/MM - Topic header"]
    end
    DS --> P_SHORT_IN

    subgraph P_EXTRACT[TASK_EXTRACTION_SYSTEM]
        direction TB
        P_EXTRACT_IN["transcript +<br/>known_employees +<br/>meta"]
        P_EXTRACT_OUT["JSON list of tasks<br/>title/desc/owner/priority"]
    end
    TR --> P_EXTRACT_IN
    KE --> P_EXTRACT_IN
    P_EXTRACT_OUT --> TASKS_EXTRACTED

    subgraph P_VERIFY[TASK_VERIFICATION_SYSTEM]
        direction TB
        P_VERIFY_IN["transcript +<br/>existing tasks +<br/>known_employees"]
        P_VERIFY_OUT["JSON missed-task list"]
    end
    TR --> P_VERIFY_IN
    EXISTING --> P_VERIFY_IN

    subgraph P_CP_EXTRACT[COUNTERPARTY_EXTRACT_SYSTEM<br/>Pass 1]
        direction TB
        P_CP1_IN["transcript only"]
        P_CP1_OUT["JSON mentions<br/>verbatim surface forms"]
    end
    TR --> P_CP1_IN
    P_CP1_OUT --> EXTRACTED_MENTIONS

    subgraph P_CP_RESOLVE[COUNTERPARTY_RESOLVE_SYSTEM<br/>Pass 2]
        direction TB
        P_CP2_IN["mentions +<br/>full directory"]
        P_CP2_OUT["JSON mention →<br/>directory_id mapping"]
    end
    EXTRACTED_MENTIONS --> P_CP2_IN
    DIR --> P_CP2_IN

    subgraph P_CANON[CANONICALIZE_TASKS_SYSTEM<br/>Pass 3]
        direction TB
        P_CANON_IN["task list +<br/>full directory"]
        P_CANON_OUT["JSON rewritten<br/>title/description"]
    end
    TASKS_EXTRACTED --> P_CANON_IN
    DIR --> P_CANON_IN

    subgraph P_PART[PARTICIPANTS_EXTRACT_SYSTEM<br/>Zoom only]
        direction TB
        P_PART_IN["transcript +<br/>team_members"]
        P_PART_OUT["JSON team-side<br/>real_name list"]
    end
    TR --> P_PART_IN
    KE --> P_PART_IN

    subgraph P_OWNER[OWNER_SYSTEM_PROMPT<br/>TG-ingest path]
        direction TB
        P_OWN_IN["task draft +<br/>team_members"]
        P_OWN_OUT["slack_user_id of owner"]
    end
    KE --> P_OWN_IN

    classDef data fill:#1f4f2d,stroke:#2d6a40,color:#fff
```


## 6. End-to-end recording lifecycle (sequence)

How a single meeting recording flows through the system from
arrival to a confirmed Task in the operator's TG.

```mermaid
sequenceDiagram
    autonumber
    participant API as Fireflies / Zoom API
    participant L as listener (auto-poll)
    participant P as Pipeline
    participant Whisper as OpenAI Whisper
    participant LLM as OpenAI gpt-5.5
    participant Doc as Google Drive / Docs
    participant DB as Postgres
    participant Trace as traces/*.jsonl
    participant TG as Telegram

    API-->>L: new recording detected
    L->>P: process_one(meta)
    P->>API: download audio
    API-->>P: mp3/m4a bytes
    P->>Whisper: chunked transcribe + bias prompt
    Whisper-->>P: transcript_text
    P->>LLM: DETAILED_SUMMARY_SYSTEM
    LLM-->>P: detailed_summary
    P->>LLM: COUNTERPARTY_EXTRACT_SYSTEM (Pass 1)
    LLM-->>P: mentions list
    P->>LLM: COUNTERPARTY_RESOLVE_SYSTEM (Pass 2)
    LLM-->>P: mention → directory_id
    P->>DB: persist CounterpartyMention rows
    P->>LLM: TASK_EXTRACTION_SYSTEM
    LLM-->>P: raw tasks JSON
    P->>DB: insert Task rows
    P->>LLM: TASK_VERIFICATION_SYSTEM
    LLM-->>P: missed-tasks JSON
    P->>DB: append Task rows
    P->>LLM: CANONICALIZE_TASKS_SYSTEM (Pass 3)
    LLM-->>P: title/desc rewrites
    P->>DB: update Task title/description
    P->>P: dedupe by topic-prefix + desc ratio
    P->>Doc: export detailed_summary + tasks
    Doc-->>P: google_doc_url
    P->>LLM: SHORT_SUMMARY_SYSTEM
    LLM-->>P: short_summary
    Note right of P: Zoom only:<br/>PARTICIPANTS_EXTRACT_SYSTEM<br/>(team_members lookup)
    P->>TG: send short summary DM
    P->>TG: post task cards (one per Task)
    P->>Trace: per-event JSONL line<br/>(every step + LLM call)
    P->>DB: pipeline_summary log
```


## 7. What's NOT in your list

You asked: «я тут всю архитектуру покрыл?» — твои 5 пунктов покрывают
основное, но в реальной системе есть ещё несколько срезов которые
полезно держать рядом:

- **End-to-end sequence** (диаграмма §6 выше) — что в каком порядке
  происходит. Это не просто "архитектура", а timeline. Без него
  понять «где затыкается» при инциденте сложнее.
- **Listener auto-poll loops** (§4 второй блок) — фоновые тики,
  которые подтягивают данные сами. Они не вписываются в
  «pipeline service-уровень», у них своя жизнь.
- **External integrations** — кто куда HTTP-запросы делает (есть
  в §2 инфраструктуре, но имеет смысл вынести отдельно если
  будешь планировать rate-limits / quotas / retry-policies).
- **Observability / traces** — где смотреть что произошло.
  `docs/TRACES.md` уже это документирует, но связь
  «trace_event → pipeline step» хорошо бы добавить как
  отдельную диаграмму.
- **Auth & secrets** — какой контейнер видит какой ключ. Сейчас:
  `OPENAI_API_KEY` через env, Google `sa.json` через volume mount.
  Полезно для ротации ключей.
- **Migrations / schema evolution** — alembic-цепочка как граф
  (от `0001_initial` до текущего `0023_counterparty_mentions`).
  Помогает при rollback.
- **Approval / lifecycle states** — для TG-flow есть state-machine
  на ActionDraft (`proposed → confirmed/edited/ignored/expired`)
  + Task (`todo → in_progress → done/blocked`). Это отдельная
  state-diagram.

Что добавлять — на твой выбор. Минимум для healthy onboarding'а
коллеги: §1-§6 текущего файла + state-machine ActionDraft/Task.
