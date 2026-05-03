# Prompt — `CONSOLIDATE_TASKS_SYSTEM` (Pass 4)

> FR-CR-05-131 — merge sequential phases of the same
> deliverable INTO ONE task, AND split composite topics
> («Ziya/Odeya», «Lunate/Antonov + Lunate/Tokarev») into one
> task per entity.

| Field | Value |
|---|---|
| Source | [`app/services/counterparty_match.py:873`](../../app/services/counterparty_match.py) |
| Constant | `CONSOLIDATE_TASKS_SYSTEM` |
| Caller | `consolidate_tasks_via_llm(...)` |
| Wired in | `_step_consolidate_tasks` (Fireflies + Zoom) |
| LLM mode | `complete_text(response_format={"type": "json_object"})` |

## Input

User prompt holds the post-canonicalize task list:
`[id]: title // description // owner=<slack_user_id>`.

## Output contract

```json
{ "consolidated": [
  { "id": <int|null>,
    "merged_from": [<int>, ...],
    "title": "<str>",
    "description": "<str>",
    "owner": "<slack_user_id|null>",
    "priority": "low"|"medium"|"high"|"urgent" },
  ...
] }
```

- `id`: preserve the original when output is a single existing
  task. `null` for new merged or split tasks.
- `merged_from`: list of input ids this output covers.
  - For merge: `[id1, id2, ...]`.
  - For split: `[<original_id>]` (one input → many outputs).
  - For unchanged: `[<id>]`.
- `description` keeps the «<topic> - <action with details>»
  format (FR-CR-05-120).

## When to MERGE

Sequential phases of the same deliverable on the same
counterparty / topic where the second is a precondition or
continuation that adds no new operator-actionable detail.

Examples (worked into the prompt):
- «Tether — формулировка апдейта» + «Tether — отправить email во
  вторник» + «Tether — короткое сообщение в WhatsApp» → ONE.
- «Bauerdart — найти главного человека» + «Bauerdart —
  организовать встречу при согласии» → ONE.

DO NOT merge if: different counterparties, different verbs
producing different artefacts, or different owners.

## When to SPLIT

If a task's topic is a composite of two unrelated entities,
split into one task per entity. Don't split if it's genuinely
one entity («TWG global» = one fund, «Bauer/Dart» = one company
with a slash, «Schaeffler/Bosch» = contract context).

## Pipeline placement

Runs AFTER canonicalize (Pass 3) and BEFORE the dedupe + doc
export. Soft-deletes merged-away rows so the Telegram cards +
Sheets sync only see the consolidated set.
