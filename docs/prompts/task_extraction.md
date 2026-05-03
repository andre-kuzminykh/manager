# Prompt — `TASK_EXTRACTION_SYSTEM`

> FR-CR-05-120 / FR-CR-05-129 / FR-CR-05-131 — extract every
> actionable task from the meeting's detailed summary, route
> each to an owner from `team_members` based on role + notes
> rules.

| Field | Value |
|---|---|
| Source | [`app/fireflies/prompts.py:190`](../../app/fireflies/prompts.py) |
| Constant | `TASK_EXTRACTION_SYSTEM` |
| Caller | `extract_tasks(...)` |
| Wired in | `_step_extract_tasks` (Fireflies + Zoom mirror) |
| LLM mode | `complete_text(response_format={"type": "json_object"})` |
| Model | `FIREFLIES_TASKS_MODEL` (default `gpt-5.5-thinking`) |
| Reasoning | `FIREFLIES_TASKS_REASONING_EFFORT` (default `medium`) |

## Input

User prompt holds:
- `known_employees` table — `slack_user_id | display_name |
  real_name | role | notes` for every active TeamMember.
- `meeting_title`, `participants` line.
- The DETAILED summary (FR-CR-05-129 — was raw transcript, now
  «structured transcript» from Pass-1 detailed-summary).

## Output contract

```json
{ "tasks": [
  { "title": "<imperative ≤80 chars>",
    "description": "<topic - action with details, ≤350 chars>",
    "owner": "<slack_user_id|null>",
    "priority": "low"|"medium"|"high"|"urgent",
    "due_date": "<YYYY-MM-DD|null>" },
  ...
] }
```

## Operator-pinned rules

1. **THINK CAREFULLY block** (FR-CR-05-120): read the entire
   detailed summary first; expect 8-25 tasks per 30-min meeting;
   walk the `known_employees` table item-by-item for owner
   selection.
2. **Description format** (FR-CR-05-120): «<тема-или-фонд> -
   <verb action with details>», ≤350 chars, comma-separated
   multi-clauses OK. Worked examples: Schaeffler / Draper /
   QIA / Варанты.
3. **Rule 4 (FR-CR-05-131)** — universal owner-routing gate:
   when assigning fundraising / IR tasks, prefer team_members
   whose `role` or `notes` mention IR / Investor Relations /
   fundraising; do NOT default to admins. Operator's previous
   regression «Алине поручено → admin» is fixed by filling
   role + notes in the Team sheet.
4. **MAXIMUM DETAIL** (FR-CR-05-129): the descriptions feed
   directly into the operator's TG cards + Sheets. Don't
   compress.

## Pipeline placement

Runs AFTER Pass 1+2 (counterparty match) and BEFORE Pass 3
(canonicalize). The freshly-extracted tasks still carry
phonetic / mangled counterparty names; canonicalize fixes them.
Verifier (`TASK_VERIFICATION_SYSTEM`) is a SECOND pass that
adds anything Pass 1 missed.
