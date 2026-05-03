# Prompt — `task_dedup._SYSTEM_PROMPT`

> Decide if a NEW task duplicates any of up to 10 existing
> backlog items — the LLM-side dedup gate that runs after the
> rule-based id / title checks.

| Field | Value |
|---|---|
| Source | [`app/services/task_dedup.py:85`](../../app/services/task_dedup.py) |
| Constant | `_SYSTEM_PROMPT` (module-private) |
| Caller | `is_duplicate_via_llm(candidate, existing)` |
| Wired in | Slack mention → draft creation; meeting `_step_dedupe_tasks` |
| LLM mode | `call_tool(tool_name="record_dedup_decision", …)` |

## Input

User prompt holds:
- `candidate`: `{ title, description }` of the new task.
- `existing`: list of up to 10 prior tasks from the same
  backlog with `title`, `description`, `owner_display_name`,
  `due_date`.

## Output

```json
{ "is_duplicate": <bool>, "duplicate_of_id": <int|null>,
  "reason": "<str>" }
```

## Operator-pinned rules (excerpt)

1. Compare DESCRIPTIONS, not just titles. The LLM should look
   for «same work, different phrasing» rather than literal
   string overlap.
2. Owner mismatch alone is NOT enough to dedupe (could be a
   reassignment); description match wins.
3. Due-date drift alone is NOT enough either.
4. Sibling drafts in the same batch (D# prefix) participate in
   the dedup window — FR-CR-05-13.

## Hallucination guard

The hub-side caller validates `duplicate_of_id` against the
union of Task ids and Draft D# ids passed in. Invented ids are
dropped (FR-CR-05-13).
