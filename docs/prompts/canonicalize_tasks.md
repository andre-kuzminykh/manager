# Prompt — `CANONICALIZE_TASKS_SYSTEM` (Pass 3)

> FR-CR-05-130 — rewrite Task titles + descriptions so every
> counterparty mention uses the canonical name from the
> directory, replacing «Тезер» / «teaser» / «BowerDart» with
> «Tether» / «Bauerdart» **without regex band-aids**
> (operator-pinned: «без regexp, как универсальное решение»).

| Field | Value |
|---|---|
| Source | [`app/services/counterparty_match.py:813`](../../app/services/counterparty_match.py) |
| Constant | `CANONICALIZE_TASKS_SYSTEM` |
| Caller | `canonicalize_task_content_via_llm(...)` |
| Wired in | `_step_canonicalize_task_names` (Fireflies + Zoom) |
| LLM mode | `complete_text(response_format={"type": "json_object"})` |

## Input

User prompt holds:
- `directory`: `id | name` of every known counterparty
  (FR-CR-05-132 — `type` removed).
- `tasks`: numbered list `[id]: title // description` from the
  freshly-extracted Task rows.

## Output contract

```json
{ "rewritten": [
  { "id": <int>, "title_rewritten": "<str>",
    "description_rewritten": "<str>" },
  ...
] }
```

- One entry per input task, **same order**.
- `id` matches the input task id (stable round-trip).
- Preserve everything that's NOT a counterparty mention
  (actions, owners, dates, amounts) verbatim.

## Operator-pinned rules

1. Replace ONLY phonetic / mangled / Cyrillic variants of
   counterparty names with the canonical Latin name from the
   directory.
2. Don't change verbs, dates, amounts, slack_user_ids, or the
   «<topic> - <action>» description format (FR-CR-05-120).
3. If a task references no counterparty, return it unchanged.

## Why it replaces fuzzy regex

Earlier `fuzzy_extend_canonical_map` used Python's
`SequenceMatcher` + regex word-boundary substitutions. Operator
regression: «BowerDart» / «Jamal» / multi-word composites kept
slipping through ad-hoc fuzzy thresholds. Pass 3 LLM with
reasoning solves them universally without per-case regex.
