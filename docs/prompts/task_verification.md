# Prompt — `TASK_VERIFICATION_SYSTEM`

> FR-CR-05-121 — second-pass verifier: re-read the transcript +
> the already-extracted task list, add anything the first pass
> missed. NO duplication of existing tasks.

| Field | Value |
|---|---|
| Source | [`app/fireflies/prompts.py:487`](../../app/fireflies/prompts.py) |
| Constant | `TASK_VERIFICATION_SYSTEM` |
| Caller | `verify_tasks(...)` |
| Wired in | `_step_verify_tasks` (Fireflies + Zoom mirror) |
| LLM mode | `complete_text(response_format={"type": "json_object"})` |

## Input

User prompt holds:
- The DETAILED summary (same as Pass-1 extract).
- `existing_tasks`: the list emitted by `_step_extract_tasks`.
- `known_employees` table.

## Output contract

```json
{ "tasks": [
  { "title": "...", "description": "...", "owner": "...",
    "priority": "...", "due_date": "..." },
  ...
] }
```

- Empty `{ "tasks": [] }` is valid (Pass 1 caught everything).
- Each entry is a NEW task that Pass 1 missed.
- Same description format as `TASK_EXTRACTION_SYSTEM`.

## Operator-pinned rules

1. «SECOND-PASS verifier» framing — distinct from the main
   extractor, the prompt explicitly tells the model to look
   for what got skipped.
2. Don't duplicate existing tasks (the verifier sees them).
3. Reuse the FR-CR-05-120 description format + rule 6
   (anti-admin-default) + rule 7 (named-assignee).

## Effect on the pipeline

New rows from this pass:
- get `reason="*_verified"` on their `TaskStatusHistory`,
- get scheduled for Sheets sync,
- get DM-card-posted by `_step_post_task_cards` after the
  short summary.
