# SPEC — Task Vector Layer (FR-TV) v0.1

**Status:** P5 deployed (read-only) 2026-06-02 · **Owner:** admin@andre.technology · **Date:** 2026-06-01 (rev. 2026-06-02)
**Epic ID:** `FR-TV` (Task Vector) · **Feature flags:** `TASK_VECTOR_ENABLED` (layer, default **off**;
**on** in prod since 2026-06-02) + `TASK_VECTOR_WRITES_ENABLED` (writers, default **off**; still off — gates P6)

---

## 0. Problem / Goal / Non-goals

**Goal.** Make the operator's tasks **semantically searchable and conversationally
operable** through the existing CEO-brain agent:

1. **Search** — «какие задачи по найму висят?» → vector search over tasks.
2. **Q&A** — «что на Семёне и что просрочено?» → search + read live fields, answer.
3. **Field update by natural language** — find the matching task by vector, then update:
   - **status** — «отправил письмо Семёну» → `done`;
   - **due date** — «перенеси задачу X на пятницу / срок до 10 июня» → new `due_date`;
   - **owner / responsible** — «переназначь на Семёна / ответственный теперь Семён» →
     resolve the person (vector over the **team**) → new `owner_user_id`/`owner_display_name`.
   Each applies immediately on a confident single match, propagates to Google Sheets/Tasks +
   cards, and is undoable.
4. **Team in the vector DB** — team members (and Slack employees) are indexed alongside tasks
   so people can be resolved by name/role for owner updates and answered-about.

**Non-goals (v0.1).**
- No new task CREATION via vector (creation stays in the meeting/Slack pipelines).
- No change to task schema beyond an optional embedding-staleness touch.
- No bulk/mass status mutation from one utterance (one task per confirmed action).
- No exposure as a standalone external MCP **server** (deferred — see DEC-4).

**Reuse (from codebase map 2026-06-01).** `entity_embeddings` table + `search_entities` /
`refresh_embeddings` (`app/services/entity_embeddings.py`); the separate pgvector DB
(`app/db.py:get_catalog_session_factory`); the CEO-brain local tool registry
(`app/ceo_brain/slack_tools.py` pattern, merged in `responder.py`); task status +
history + sync (`app/models/task.py`, `app/sync/{task_sync,tasks_api,sheets}.py`).

---

## 1. Architecture

```
                       ┌──────── INDEXING (background, idempotent) ────────┐
  Task (DB, primary) ──┤ build_text_repr_task() → hash → embed (3072) →     │
   create/edit/status  │ upsert entity_embeddings(kind='task') in VECTOR DB │
                       └────────────────────────────────────────────────────┘
                                            │  (separate pgvector instance)
  operator utterance                        ▼
  «отправил письмо Семёну»   ┌── CEO-brain (Anthropic, MCP client) ──┐
        │                    │  local tools:                          │
        ▼                    │   • search_tasks(query,k,filters)──────┼─► vector search
   Claude decides ──────────►│   • get_task(id)                       │   (kind='task')
                             │   • update_task_status(id,status,...)──┼─► DB update →
                             │  (confirm before any write)            │   history + sync
                             └────────────────────────────────────────┘   (Sheets/GTasks/cards)
```

- **Embedded text (`text_repr`)** = **content only**: `title + description + owner_display_name
  + category`. **Status / due_date are NOT embedded** — they are volatile and are read
  **live from the Task row at query time** (search returns `entity_id` → load Task).
  Rationale: status changes must not force re-embeds nor distort semantic similarity.
- **Storage**: `entity_embeddings.kind='task'`, `entity_id = str(task.id)`, in the vector
  DB selected by `TASK_VECTOR_DATABASE_URL` (falls back to `CATALOG_DATABASE_URL`, then
  primary). Prod transactional DB is never required to host pgvector.
- **Freshness**: a cron (`ops.refresh_task_embeddings`, idempotent via `text_repr_hash`)
  re-embeds changed/new tasks and prunes deleted ones; PLUS an optional best-effort
  immediate upsert hook on task create/update (FR-TV-013).

---

## 2. Design decisions (with rationale)

| ID | Decision | Rationale |
|----|----------|-----------|
| **DEC-1** | Tasks indexed as `kind='task'` in the existing `entity_embeddings`, in the **separate** vector DB. | Reuses proven stack; keeps prod DB free of pgvector (operator decision 2026-06-01). |
| **DEC-2** | Embed **content only**; read status/due **live**. | Status churn shouldn't re-embed or skew search; live read = always correct status. |
| **DEC-3** | Status update applies **immediately on a confident single match** (no «да?» confirmation), one task per action, with **undo** + audit. The ambiguity gate still asks when 2+ match or confidence is low. | Operator choice 2026-06-01 «меняй сразу». Undo + allow-list + ambiguity gate are the safety net replacing the confirmation step. |
| **DEC-4** | Expose task tools **both** as CEO-brain local tools **and** as an external **MCP server** (for an external Claude client). | Operator choice 2026-06-01 «да, ещё и для внешнего Клода». Same executors behind both surfaces. → FR-TV-090. |
| **DEC-5** | NL→status mapping by a fixed **verb→status table** (below), applied by the agent. | Operator choice 2026-06-01 «да». Deterministic, reviewable. |
| **DEC-6** | Person reference («Семёну») handled two ways: owner is in `text_repr` (cheap recall) **and** optional owner-filter via person resolution. | Robust without a hard dependency on person-matcher. |
| **DEC-7** | Feature gated by `TASK_VECTOR_ENABLED` (off) + reversible; the flag is flipped **last**, after indexing + calibration (operator «фича флаг в конце»). | Same safe-rollout discipline as FR-CR-05-241. |
| **DEC-8** | The **team** is indexed in the same vector instance as `kind='team_member'` (+ `kind='employee'`), reusing `build_text_repr_team_member`/`_employee` + `refresh_embeddings`. | People resolution for owner updates + Q&A; zero new infra. |
| **DEC-9** | Update tools cover **status, due_date, owner**. **Undo** is generic: every update returns `{field, from, to}`; undo re-applies `from` via the same tool. | One pattern for all field updates; no new history table for due/owner. |
| **DEC-10** | Owner update resolves the person by **vector over the team** (`kind in team_member,employee`) + the same τ/δ ambiguity gate. | «ответственный — Семён» needs name→person; reuse the search stack. |

**Verb→status table (DEC-5, OQ-2 resolved).** Case-insensitive stem match on the
utterance's action verb:

| Russian verb stem (examples) | → target status |
|---|---|
| отправил / сделал / закрыл / завершил / готово / выполнил / отдал / сдал | `done` |
| начал / в работе / приступил / делаю / занимаюсь | `in_progress` |
| (no recognised action verb) | no status inferred → agent asks |

Unknown/ambiguous verb ⇒ no inference; agent asks. The mapping is a constant in
`task_tools` (`VERB_STATUS_MAP`) so it's unit-testable and reviewable.

**Intent table (status / due / owner).** The agent classifies the utterance's intent and
routes to one update tool:

| Intent cue (examples) | → tool | extracted arg |
|---|---|---|
| отправил/сделал/закрыл/начал… | `update_task_status` | target status (verb→status above) |
| перенеси/срок/дедлайн/до <дата>/на <день недели> | `update_task_due` | parsed `due_date` (+`due_time`?) |
| переназначь/ответственный/назначь/на <имя> | `update_task_owner` | resolved person (vector over team) |

Date parsing («на пятницу», «до 10 июня», «завтра») is done by the agent into an ISO date
passed to `update_task_due`; the tool validates the ISO date (no NL parsing inside the tool).

---

## 3. User Stories (`US-TV-*`)

- **US-TV-01** — *As the operator*, I ask the agent in plain language to find tasks
  by topic/person, so I don't scroll the sheet. → FR-TV-020, FR-TV-021.
- **US-TV-02** — *As the operator*, I ask «что просрочено / что на Семёне», and get a
  correct, current answer. → FR-TV-030.
- **US-TV-03** — *As the operator*, I say «отправил письмо Семёну» and the matching task
  flips to done without me finding its id. → FR-TV-040..045.
- **US-TV-04** — *As the operator*, when my phrasing matches several tasks, the agent asks
  which one instead of guessing. → FR-TV-043.
- **US-TV-05** — *As the operator*, a status change I make via chat shows up in the Google
  Sheet, Google Tasks and the Slack card, like any other change. → FR-TV-050.
- **US-TV-06** — *As the operator*, newly created/edited tasks become findable within a
  short, known window. → FR-TV-013, NFR-TV-003.
- **US-TV-07** — *As an admin*, only authorized users can change task status via chat,
  and every change is audited. → FR-TV-060, NFR-TV-006.
- **US-TV-08** — *As an operator*, if the vector layer is down, normal task flows keep
  working and the agent says it can't search rather than crashing. → NFR-TV-005.
- **US-TV-09** — *As the operator*, I say «перенеси задачу про лендинг на пятницу» and the
  matching task's due date moves, syncing everywhere. → FR-TV-047.
- **US-TV-10** — *As the operator*, I say «ответственный за договор теперь Семён» and the
  task's owner changes after the person is resolved from the team. → FR-TV-048, FR-TV-024.
- **US-TV-11** — *As the operator*, I ask «кто такой Семён / какие задачи на нём», and the
  team (people) is searchable in the same vector layer. → FR-TV-015, FR-TV-024.

---

## 4. Use Cases (`UC-TV-*`)

### UC-TV-01 — Semantic task search
- **Actor:** operator (via CEO-brain). **Pre:** `TASK_VECTOR_ENABLED=on`, index fresh.
- **Main:** utterance → `search_tasks(query,k)` → top-K `entity_id` → load live Task rows →
  agent answers with title/status/owner/due/permalink.
- **Alt:** filters (status/owner/overdue) applied post-retrieval.
- **Error:** vector DB unreachable → tool returns `{error}`, agent says it can't search now;
  pipeline unaffected.

### UC-TV-02 — Q&A over tasks
- Compose `search_tasks` + live fields; agent aggregates (e.g., overdue = due_date<today AND
  status≠done). **Error:** empty → «не нашёл задач по …».

### UC-TV-03 — Status update by natural language
- **Main:** utterance «<action> <person/topic>» → agent infers target status (DEC-5) →
  `search_tasks` → **single** top candidate with score ≥ τ_high → apply immediately (DEC-3) →
  `update_task_status(id,new_status,reason)` → DB update + history + sync → agent reports «X→done
  (отменить?)».
- **Alt-ambiguous:** ≥2 candidates within δ of top OR top score ∈ [τ_low, τ_high) → agent
  lists candidates, asks operator to pick; no write until chosen.
- **Alt-already-in-state:** task already `done` → no-op, agent says so (idempotent).
- **Error-none:** best score < τ_low → no write; agent says «не нашёл подходящую задачу».
- **Error-unauthorized:** caller ∉ allowed users → refused, audited.

### UC-TV-04 — Due-date update by natural language
- **Main:** «перенеси <topic> на <date>» → agent parses date→ISO → `search_tasks` → single
  confident task → `update_task_due(id, due_date_iso, reason)` → validate ISO → set `due_date`
  → sync → report «срок X → <date> (отменить?)».
- **Alt/Errors:** same ambiguity / none / unauthorized gates as UC-TV-03. Invalid date → no write.

### UC-TV-05 — Owner (responsible) update by natural language
- **Main:** «ответственный за <topic> — <name>» → `search_tasks` → single confident task →
  `resolve_person(name)` = vector over `kind in {team_member, employee}` → single confident
  person → `update_task_owner(id, owner_user_id, owner_display_name, reason)` → sync → report
  «ответственный X → <name> (отменить?)».
- **Alt-ambiguous-task / ambiguous-person:** either side ambiguous/low-conf → agent asks; no
  write. **Error-person-not-found:** no person ≥ τ_low → no write, agent says so.

---

## 5. Functional Requirements (`FR-TV-*`)

### 5.1 Indexing
- **FR-TV-010** — `build_text_repr_task(title, description, owner_display_name, status, due_date,
  category)` returns a deterministic string of **content** fields (title, description, owner,
  category). Status/due MAY appear only as non-semantic tail or be omitted; they are not the
  match signal. Empty/whitespace-only → entity not embeddable (skipped).
- **FR-TV-011** — `refresh_task_embeddings` embeds tasks where `deleted_at IS NULL`, upserts
  `entity_embeddings(kind='task', entity_id=str(task.id), model, dim=3072, text_repr,
  text_repr_hash)`, and **skips** rows whose `text_repr_hash` is unchanged (idempotent, zero
  OpenAI calls when nothing changed).
- **FR-TV-012** — Deleted/soft-deleted tasks are **pruned** from the index on refresh.
- **FR-TV-013** — *(flag `TASK_VECTOR_IMMEDIATE_UPSERT`, **NOT YET IMPLEMENTED** as of
  2026-06-02)* on task create/status/edit, a best-effort immediate upsert SHOULD run; failure
  swallowed (cron is the backstop). The config field exists but has no create/edit hook yet —
  setting it is a no-op. Freshness is currently the 10-min cron only (operator choice
  2026-06-02: «cron every 10 min»).
- **FR-TV-013a** — `refresh_embeddings_cross_db` supports a partial `task_created_since` window
  (CLI `--since YYYY-MM-DD`). A windowed scan MUST NOT prune (it is a deliberate subset);
  `prune = (task_created_since is None)`. Only a full scan reconciles deletions.
- **FR-TV-014** — Re-embed is triggered only by a change in the **content** text_repr; a pure
  status change does NOT re-embed (DEC-2).
- **FR-TV-015 (TEAM INDEX)** — The same refresh indexes the **team** into the vector instance:
  `kind='team_member'` (from `TeamMember`, via `build_text_repr_team_member`) and
  `kind='employee'` (from `Employee`, via `build_text_repr_employee`). Idempotent + pruned like
  tasks. This is what makes owner resolution + people Q&A possible (DEC-8).

### 5.2 Search
- **FR-TV-020** — `search_tasks(query, k=TASK_VECTOR_K, status?, owner?, overdue?)` →
  list of `{task_id, title, status, owner_display_name, due_date, score, permalink?}`,
  ordered by cosine score desc; status/owner/due are read **live** from the Task row.
- **FR-TV-021** — Post-retrieval filters: `status` (exact), `owner` (resolved id or display),
  `overdue` (due_date<today AND status≠done). Filters never invent rows.
- **FR-TV-022** — Soft-deleted tasks never appear in results (defense-in-depth even if index
  lagged).
- **FR-TV-023** — Empty query or no candidates → `[]` (tool returns empty, not error).
- **FR-TV-024 (PERSON RESOLUTION)** — `resolve_person(name, k)` → vector search over
  `kind in {team_member, employee}` → `[{person_id, display_name, role, score}]` ordered by
  score. Used by owner updates and people Q&A. Applies the same τ/δ confidence gate; returns
  `[]` when nothing ≥ τ_low. `person_id` is the owner key written to the task
  (`team_member`→a stable user id / `employee`→`slack_user_id`).

### 5.3 Q&A
- **FR-TV-030** — The agent answers task questions strictly from `search_tasks`+live fields;
  it must not fabricate tasks. (System-prompt rule, mirrors FR-CB2-3.14.)

### 5.4 Status update
- **FR-TV-040** — `update_task_status(task_id, new_status, reason, actor_id)`:
  validates `new_status ∈ {backlog,todo,in_progress,done}` and the transition is allowed
  (per `app/models/task.py` lifecycle), sets `status`, `completed_at`/`started_at` as per
  existing rules, writes a `TaskStatusHistory` row (`from_status`,`to_status`,`changed_by`,
  `reason`), and triggers `TaskSyncer.sync(task_id)`.
- **FR-TV-041** — Invalid/unknown `task_id` → no write, structured `{error:"not_found"}`.
- **FR-TV-042** — Invalid `new_status` or disallowed transition → no write,
  `{error:"invalid_transition", from, to}`.
- **FR-TV-043** — Confidence/ambiguity gate (DEC-3): when exactly ONE candidate has score
  ≥ τ_high and no other is within δ, the agent applies the change **immediately** (no
  confirmation). When ≥2 are within δ, or the top is in [τ_low, τ_high), the agent lists
  candidates and asks; no write. When < τ_low, no write. The tool stays id-precise (cannot
  mass-update).
- **FR-TV-044** — Idempotent: updating to the current status is a no-op success
  (`{ok:true, changed:false}`), no duplicate history row.
- **FR-TV-045** — On success the index entry for that task is refreshed only if content
  changed (status-only change ⇒ no re-embed, FR-TV-014); response includes the new status +
  task title for the agent to echo.
- **FR-TV-046 (UNDO, generic)** — Because writes apply without confirmation, every auto-applied
  update (status/due/owner) is reversible (DEC-9): each update tool returns
  `{field, from, to, task_id}`; the agent surfaces «изменил <field> X→Y (отменить?)» and an undo
  re-applies `from` via the same tool (status also has `TaskStatusHistory` as a second source).
  Undo is itself audited.
- **FR-TV-047 (UPDATE DUE)** — `update_task_due(task_id, due_date, reason, actor_id[, due_time])`:
  validates `due_date` is a real ISO date (the tool does NOT parse natural language — the agent
  supplies ISO), sets `due_date`(/`due_time`), recomputes `is_current_week` per existing rules,
  triggers `TaskSyncer.sync`. Returns `{field:'due_date', from, to}`. Invalid date → no write,
  `{error:'invalid_date'}`. No re-embed (due not in content). Allow-list + audit as FR-TV-060/061.
- **FR-TV-048 (UPDATE OWNER)** — `update_task_owner(task_id, owner_user_id, owner_display_name,
  reason, actor_id)`: sets `owner_user_id`+`owner_display_name`, updates subscriptions per
  existing rules, triggers sync. Returns `{field:'owner', from, to}`. Owner CONTENT changed ⇒
  the task IS re-embedded on next refresh (owner is in `text_repr`, FR-TV-010). Unknown/empty
  owner → no write. Allow-list + audit. The person is resolved upstream via FR-TV-024 (the tool
  takes an already-resolved id; the ambiguity gate lives in the agent).

### 5.5 Tools / agent integration
- **FR-TV-070** — New module `app/ceo_brain/task_tools.py` exposes `TASK_TOOL_SCHEMAS`
  (`search_tasks`, `get_task`, `resolve_person`, `update_task_status`, `update_task_due`,
  `update_task_owner`) and `build_task_executors(session_factory, settings)` returning
  `{name: callable(input)->json_str}`, mirroring `slack_tools.py`. All four update/resolve tools
  share the τ/δ gate, allow-list and audit.
- **FR-TV-071** — `responder.build_anthropic_request` merges task tools into `tools=[...]`
  ONLY when `TASK_VECTOR_ENABLED`. System prompt gains task-tool routing hints (the write-tool
  hints are themselves gated by `TASK_VECTOR_WRITES_ENABLED`, FR-TV-073).
- **FR-TV-073 (READ-ONLY-FIRST GATE)** — A second flag `TASK_VECTOR_WRITES_ENABLED` (default
  **off**) lives INSIDE `TASK_VECTOR_ENABLED`. When off, `build_task_executors(writes_enabled=
  False)` returns only `{search_tasks, get_task, resolve_person}` and `task_tool_schemas(
  writes_enabled=False)` omits the three `update_*` schemas — so the writers are absent from the
  agent's tool set entirely (cannot be hallucinated into existence). Same gate applies to the
  external MCP server (FR-TV-090). This lets search/Q&A go live (P5) before τ/δ is calibrated;
  writes turn on only at P6. Read tools are always `READ_TOOL_NAMES`; writers `WRITE_TOOL_NAMES`.
- **FR-TV-072** — `update_task_status` executor is id-precise and writes when called (no
  confirmation step, DEC-3). Safety is enforced by: the ambiguity gate FR-TV-043 (agent only
  auto-calls on a single confident match), the allow-list FR-TV-060, undo FR-TV-046, and audit
  FR-TV-061.
- **FR-TV-090 (EXTERNAL MCP SERVER)** — The same executors are exposed via a standalone MCP
  server (`ops.task_mcp_server` / `app/mcp/task_server.py`) speaking MCP over stdio/HTTP, so an
  external Claude client can call `search_tasks` / `get_task` / `update_task_status` /
  `undo_last_task_status`. The server reuses `build_task_executors` (single source of truth),
  enforces the SAME allow-list + ambiguity discipline, is gated by `TASK_MCP_SERVER_ENABLED`,
  and authenticates the client (token). Tool schemas are shared with the local CEO-brain
  registry.

### 5.6 Sync
- **FR-TV-050** — A status change via `update_task_status` propagates identically to a manual
  change: DB → Google Sheets row → Google Tasks (`needsAction`/`completed`) → Slack/TG card
  refresh, via the existing `TaskSyncer`. Google-side failure is best-effort (logged).
- **FR-TV-051** — A status change made externally (Sheet/Google Tasks pull) does NOT need an
  immediate re-embed (content unchanged); the next cron is sufficient.

### 5.7 Security / audit
- **FR-TV-060** — `update_task_status` is allowed only for `actor_id ∈ CEO_BRAIN_ALLOWED_USERS`
  (reuse existing allow-list); otherwise refused with `{error:"forbidden"}` and an audit log.
- **FR-TV-061** — Every status update writes `TaskStatusHistory` and is captured by the
  responder's `persist_run` audit (who/when/utterance).

---

## 6. Non-Functional Requirements (`NFR-TV-*`)

- **NFR-TV-001 Latency** — `search_tasks` p95 < 1.5 s for ≤ a few-thousand tasks (exact cosine
  scan, no ANN); `update_task_status` p95 < 2 s excluding Google API.
- **NFR-TV-002 Cost** — search = 1 embedding call/query; no LLM in the tool itself. Index
  refresh = 0 OpenAI calls when nothing changed (hash gate). Stays within
  `CEO_BRAIN_MAX_RUN_COST_USD`.
- **NFR-TV-003 Freshness** — a new/edited task is searchable within ≤ `refresh interval`
  (deployed: 10-min cron, 2026-06-02). `TASK_VECTOR_IMMEDIATE_UPSERT` (near-instant) is spec'd
  but NOT yet implemented (FR-TV-013) — cron is the sole mechanism today.
- **NFR-TV-004 Accuracy** — precision-first for status writes. τ_high/τ_low/δ tunable via
  settings, calibrated on a **synthetic** dataset (OQ-5 resolved: no human-labelled set
  available). A generator (`ops.gen_task_vector_eval`) produces (utterance → expected task)
  pairs from real task titles with paraphrase/typo/transliteration noise; thresholds chosen to
  maximise precision@1 with high recall on that set before any auto-write is enabled.
- **NFR-TV-005 Resilience** — vector DB / OpenAI failure NEVER breaks task pipelines or the
  agent; tools degrade to `{error}` and the agent explains. Mirrors the never-raise discipline
  of `counterparty_shadow_v2`.
- **NFR-TV-006 Security/Privacy** — only allow-listed users mutate; reads scoped to the org's
  tasks; secrets via env only.
- **NFR-TV-007 Idempotency** — repeated identical updates/refreshes produce no duplicates.
- **NFR-TV-008 Observability** — structured logs: `task_index_refresh{scanned,embedded,skipped,
  pruned}`, `task_search{query,k,hits,seconds}`, `task_status_update{task_id,from,to,actor,ok}`.
- **NFR-TV-009 Reversibility** — `TASK_VECTOR_ENABLED=off` fully removes task tools; no schema
  migration is required to disable (index rows are inert).

---

## 7. BDD scenarios (`SC-TV-*`, Gherkin)

```gherkin
# SC-TV-01  (FR-TV-020) happy search
Given tasks are indexed (kind='task') and TASK_VECTOR_ENABLED is on
When the operator asks "какие задачи по найму"
Then search_tasks returns matching tasks ordered by score
And each result shows the task's LIVE status, owner and due_date

# SC-TV-02  (FR-TV-030) Q&A overdue
Given indexed tasks, some with due_date < today and status != done
When the operator asks "что просрочено"
Then only those tasks are listed, computed from live fields

# SC-TV-03  (FR-TV-040,050) status update happy
Given exactly one task clearly matches "письмо Семёну" (score >= τ_high)
And the operator is in CEO_BRAIN_ALLOWED_USERS
When the operator says "отправил письмо Семёну"
And confirms the proposed task
Then update_task_status sets it to done, writes TaskStatusHistory
And the change syncs to Google Sheets, Google Tasks and the card

# SC-TV-04  (FR-TV-043) ambiguous → ask, no write
Given two tasks match "письмо Семёну" within δ of the top score
When the operator says "отправил письмо Семёну"
Then the agent lists both and asks which one
And no status is changed until the operator picks

# SC-TV-05  (FR-TV-040 none) no confident match
Given no task scores >= τ_low for the utterance
When the operator says "отправил отчёт в налоговую"
Then no write happens and the agent says it found no matching task

# SC-TV-06  (FR-TV-044) idempotent
Given the matched task is already done
When the operator says "отправил письмо Семёну"
Then the agent reports it's already done and writes no new history row

# SC-TV-07  (FR-TV-042) invalid transition
Given a task in status backlog
When update_task_status is asked for an unknown status "shipped"
Then it returns invalid_transition and changes nothing

# SC-TV-08  (FR-TV-060) unauthorized
Given a user not in CEO_BRAIN_ALLOWED_USERS
When they try to change a task status via chat
Then the update is refused and audited

# SC-TV-09  (FR-TV-012,022) deleted task not surfaced
Given a task was soft-deleted
When search runs
Then the deleted task never appears (pruned on refresh, filtered at query)

# SC-TV-10 (NFR-TV-005) vector DB down
Given the task vector DB is unreachable
When the operator searches tasks
Then search_tasks returns an error, the agent explains, nothing crashes

# SC-TV-11 (FR-TV-011) idempotent refresh
Given no task content changed since last refresh
When refresh_task_embeddings runs
Then it makes zero OpenAI calls and embeds nothing

# SC-TV-12 (FR-TV-014) status-only change skips re-embed
Given a task's status changes but its content does not
When refresh runs
Then its embedding is not recomputed

# SC-TV-13 (FR-TV-047) due-date update
Given exactly one task matches "лендинг" with score >= τ_high
When the operator says "перенеси лендинг на пятницу"
Then the agent parses Friday to an ISO date and calls update_task_due
And the due_date changes and syncs, with an undo offer

# SC-TV-14 (FR-TV-048,024) owner update via person resolution
Given exactly one task matches "договор" and the team is indexed
When the operator says "ответственный за договор теперь Семён"
And resolve_person("Семён") yields a single person with score >= τ_high
Then update_task_owner sets that owner and syncs, with an undo offer

# SC-TV-15 (FR-TV-024) ambiguous person → ask
Given two team members match "Семён" within δ
When the operator reassigns a task to "Семён"
Then the agent lists both people and asks which, and no owner is written

# SC-TV-16 (FR-TV-024) person not found
Given no team member scores >= τ_low for the name
When the operator reassigns to that name
Then no write happens and the agent says the person was not found

# SC-TV-17 (FR-TV-015) team indexed
Given team members and employees exist
When refresh runs
Then kind='team_member' and kind='employee' embeddings are upserted

# SC-TV-18 (FR-TV-046) undo a due/owner change
Given the agent just changed a task's due_date X->Y
When the operator says "отмени"
Then the agent re-applies X via update_task_due
```

---

## 8. Test catalog (`T-*`)

Naming: `T-FR-TV-0NN-x` (unit/functional) · `T-SC-TV-NN` (BDD scenario) · pgvector-gated
tests skip when `TEST_PG_VECTOR_URL` unset (house style, `test_entity_embeddings_service.py`).

| Test ID | Covers | Type | Notes |
|---|---|---|---|
| T-FR-TV-010-a | FR-TV-010 | unit | text_repr deterministic; content-only; empty→not embeddable |
| T-FR-TV-011-a | FR-TV-011 | pg | upsert + hash skip (idempotent, 0 calls on no-change) |
| T-FR-TV-012-a | FR-TV-012 | pg | soft-deleted pruned |
| T-FR-TV-014-a | FR-TV-014 | unit | status-only change ⇒ same hash ⇒ no re-embed |
| T-FR-TV-020-a | FR-TV-020 | pg | search returns live status/owner/due, ordered by score |
| T-FR-TV-021-a | FR-TV-021 | unit | filters (status/owner/overdue) post-retrieval |
| T-FR-TV-022-a | FR-TV-022 | unit | deleted never surfaced even if index lags |
| T-FR-TV-023-a | FR-TV-023 | unit | empty query/no candidates → [] |
| T-FR-TV-040-a | FR-TV-040 | unit | valid transition → status+history+sync called |
| T-FR-TV-041-a | FR-TV-041 | unit | unknown id → not_found, no write |
| T-FR-TV-042-a | FR-TV-042 | unit | invalid status/transition → no write |
| T-FR-TV-044-a | FR-TV-044 | unit | update to same status → no-op, no dup history |
| T-FR-TV-045-a | FR-TV-045 | unit | response carries new status+title |
| T-FR-TV-050-a | FR-TV-050 | unit | TaskSyncer.sync invoked on update (mocked) |
| T-FR-TV-060-a | FR-TV-060 | unit | non-allow-listed actor refused + audited |
| T-FR-TV-015-a | FR-TV-015 | pg | team_member + employee embeddings upserted/pruned |
| T-FR-TV-024-a | FR-TV-024 | unit | resolve_person ranks team; []< τ_low; ambiguity flagged |
| T-FR-TV-047-a | FR-TV-047 | unit | valid ISO due set+sync; invalid date→no write |
| T-FR-TV-048-a | FR-TV-048 | unit | owner set+sync; returns {field:'owner',from,to}; re-embed flagged |
| T-FR-TV-046-a | FR-TV-046 | unit | each update returns {field,from,to}; undo re-applies `from` |
| T-FR-TV-070-a | FR-TV-070 | unit | task_tools schemas valid (6 tools); executors map present |
| T-FR-TV-071-a | FR-TV-071 | unit | tools merged only when TASK_VECTOR_ENABLED |
| T-FR-TV-073-a | FR-TV-073 | unit | writes off (default) ⇒ executors = read-only 3; no update_* |
| T-FR-TV-073-b | FR-TV-073 | unit | task_tool_schemas(writes_enabled) read-only vs full subset |
| T-FR-TV-013a-a | FR-TV-013a | pg | `--since` window does NOT prune rows outside the window |
| T-SC-TV-14 | SC-TV-14 | integ | owner update via person resolution (fakes) |
| T-SC-TV-15 | SC-TV-15 | integ | ambiguous person → ask, no write |
| T-SC-TV-03 | SC-TV-03 | integ | happy NL update end-to-end (fakes) |
| T-SC-TV-04 | SC-TV-04 | integ | ambiguity gate: no write, asks |
| T-SC-TV-05 | SC-TV-05 | integ | no-match: no write |
| T-SC-TV-10 | SC-TV-10/NFR-TV-005 | unit | vector down → {error}, never raises |

---

## 9. Scenario matrix (status update)

| Case | Top score | #within δ | Authorized | Outcome |
|---|---|---|---|---|
| Happy | ≥ τ_high | 1 | yes | confirm → update done |
| Ambiguous | ≥ τ_high | ≥2 | yes | list, ask, no write |
| Low-confidence | [τ_low, τ_high) | any | yes | propose top, ask, no write |
| No match | < τ_low | — | yes | no write, say so |
| Already in target | ≥ τ_high | 1 | yes | no-op success |
| Foreign/forbidden | any | any | **no** | refuse + audit |
| Vector down | — | — | yes | {error}, agent explains |
| Stale index (new task) | low | — | yes | none; immediate-upsert or next cron fixes |

τ_high / τ_low / δ → settings `TASK_VECTOR_TAU_HIGH` (0.45?), `_TAU_LOW` (0.30?), `_DELTA`
(0.05?) — calibrate on a labelled set (NFR-TV-004) before flipping `on`.

---

## 10. Work plan (phased, safe-rollout like FR-CR-05-241)

- **P0 — Spec & tests (this doc).** Land spec + failing/skeleton tests (the contract). *0 prod.*
- **P1 — Indexing.** `KIND_TASK`, `build_text_repr_task`, **team index** (`kind=team_member,
  employee`, FR-TV-015), `ops.refresh_task_embeddings` (cross-DB: read primary tasks/team →
  write vector instance); unit + pg tests. *0 prod.*
- **P2 — Tools.** `app/ceo_brain/task_tools.py`: `search_tasks`, `get_task`, `resolve_person`,
  `update_task_status`, `update_task_due`, `update_task_owner` (+ generic undo), `VERB_STATUS_MAP`,
  config flags; unit tests with fakes (no live DB/LLM). *0 prod.*
- **P3 — Wire (flag off).** Merge tools in `responder` behind `TASK_VECTOR_ENABLED`; system-
  prompt routing + ambiguity/confirmation rules. Deploy with flag **off**. *No behaviour change.*
- **P4 — Index build + dry search.** Point `TASK_VECTOR_DATABASE_URL` at the (existing) pgvector
  instance, run `refresh_task_embeddings`, verify search quality offline. Calibrate τ/δ.
- **P5 — Read-only enable.** ✅ DONE (2026-06-02). `TASK_VECTOR_ENABLED=on` on `manager-bot-1`
  with the **second gate** `TASK_VECTOR_WRITES_ENABLED=off` (default): only the read tools
  (`search_tasks`/`get_task`/`resolve_person`) are exposed — the three `update_*` writers are
  omitted from BOTH the executor map AND the tool schemas (responder + external MCP server), so
  the agent literally cannot invoke a write. Search + Q&A live; writes impossible until P6.
  Freshness via cron (see §Freshness below). *Search/Q&A live, 0 write risk.*
- **P6 — Writes live.** `TASK_VECTOR_WRITES_ENABLED=on` AFTER τ/δ calibration on the synthetic
  eval set (NFR-TV-004). Confirmed status/due/owner writes; watch `task_status_update` /
  `task_due_update` / `task_owner_update` logs. Reversible by clearing the flag.
- **Rollback at any phase:** `TASK_VECTOR_ENABLED=off` (+ recreate) → all task tools vanish;
  `TASK_VECTOR_WRITES_ENABLED=off` → only writers vanish (read stays). Task pipeline unchanged.
  Index rows are inert.

### Freshness (as deployed 2026-06-02)
- The index is kept current **only** by the batch `ops.refresh_task_embeddings` (idempotent via
  `text_repr_hash`: an unchanged run does `embedded=0` with **zero** OpenAI calls). Deployed as a
  cron every 10 min (`/home/andre/task_reindex.sh`, full scan = embeds new/changed + prunes
  deleted). New tasks become searchable within ≤10 min.
- **FR-TV-013 (`TASK_VECTOR_IMMEDIATE_UPSERT`) is NOT YET IMPLEMENTED** — the config flag exists
  but there is no create/edit hook; setting it is currently a no-op. The cron is the sole
  freshness mechanism. (Future work, if ≤10 min latency is insufficient.)
- **`--since YYYY-MM-DD`** (incremental task window) is supported but **never prunes**: a windowed
  scan is a deliberate SUBSET, so pruning is force-disabled when `--since` is set (a full scan,
  no `--since`, is what reconciles deletions). Regression: `--since` once pruned 3361/3655 rows;
  fixed 2026-06-02 (`prune = task_created_since is None`).

---

## 11. Resolved decisions (2026-06-01) & risks

- **OQ-1 → RESOLVED:** expose **both** internal CEO-brain tools **and** an external MCP server
  (FR-TV-090).
- **OQ-2 → RESOLVED:** fixed verb→status table (§2). 
- **OQ-3 → RESOLVED:** support both owner («Семёну») and topic («по найму») — owner + content
  in `text_repr`.
- **OQ-4 → RESOLVED:** **apply immediately** on a confident single match (no confirmation);
  ambiguity/low-confidence still asks; **undo** (FR-TV-046) + allow-list + audit are the net.
- **OQ-5 → RESOLVED:** calibrate τ/δ on a **synthetic** eval set (NFR-TV-004); no human dataset.
- **RISK-1** Index staleness → a just-created task may miss a status update → mitigated by
  immediate-upsert (FR-TV-013) / short refresh interval.
- **RISK-2** Wrong-task auto-write (raised by «меняй сразу») → mitigated by τ_high single-match
  gate + allow-list + **undo** + full audit; thresholds tuned conservatively on synthetic eval.
- **RISK-3** External MCP server widens the attack surface → token auth + allow-list on writes +
  flag-gated (`TASK_MCP_SERVER_ENABLED`, default off).
