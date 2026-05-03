# Prompt — `OWNER_SYSTEM_PROMPT`

> FR-CR-04-04 — pick a Slack user_id from `team_members` for the
> task's owner field.

| Field | Value |
|---|---|
| Source | [`app/intent/owner_prompt.py:14`](../../app/intent/owner_prompt.py) |
| Constant | `OWNER_SYSTEM_PROMPT` |
| Caller | `app/intent/classifier.py` (owner field resolver) |
| LLM mode | Anthropic `call_tool` |

## Input

User prompt holds:
- The original message + immediate context.
- `known_employees` table — `slack_user_id | display_name |
  real_name | role | notes` for every active TeamMember
  (same shape the meeting pipelines pass — FR-CR-05-123).

## Output

`{ "owner_user_id": "<slack_user_id|null>" }`.

`null` when no plausible owner is named or implied.

## Operator-pinned rules

1. **Rule 6 (FR-CR-04-04)** — anti-admin-default: never assign
   to admin slack_user_ids unless the message explicitly names
   them.
2. **Rule 7 (FR-CR-04-04)** — named-assignee wins: «Алина
   подготовит X» → owner = Алина's slack_user_id, even if the
   sentence's grammatical subject is someone else.
3. **Universal role/notes gate (FR-CR-05-131)** — when routing
   IR / fundraising tasks, prefer team members whose `role` /
   `notes` mention IR / Investor Relations / fundraising. Fix
   for «Алине поручено → admin» is in the Team sheet (fill
   role + notes), not the prompt.

## Cross-pipeline consistency (FR-CR-05-123)

Same `known_employees` shape feeds:
- TG ingest path: `OWNER_SYSTEM_PROMPT` (separate call).
- Fireflies/Zoom: `TASK_EXTRACTION_SYSTEM` (combined extract +
  route in one call).

Test pin: `test_fireflies.py::test_owner_assignment_full_team_context_consistent_across_all_paths`.
