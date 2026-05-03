# Prompt — `COUNTERPARTY_MATCH_SYSTEM` (legacy single-call)

> FR-CR-05-125 — original one-shot matcher kept for
> backwards compatibility. The two-pass replacement
> (`COUNTERPARTY_EXTRACT_SYSTEM` + `COUNTERPARTY_RESOLVE_SYSTEM`,
> FR-CR-05-129) is the active path; this prompt + helper
> remains for direct callers / fallback paths.

| Field | Value |
|---|---|
| Source | [`app/services/counterparty_match.py:35`](../../app/services/counterparty_match.py) |
| Constant | `COUNTERPARTY_MATCH_SYSTEM` |
| Caller | `match_counterparties_in_transcript(...)` |
| LLM mode | `call_tool(tool_name="record_counterparty_matches", …)` |

## Input

- Transcript (Russian + English, Whisper-noisy).
- `directory`: `id | name` (FR-CR-05-132 — `type` removed).

## Output contract

Tool call `record_counterparty_matches` with
`{ "matched_ids": [<int>, …] }`.

- Ids in the order the entity first appears in the transcript.
- Deduped — one id per entity.
- Unknown entities are silently dropped (no invented ids).
- Empty list `[]` is valid for internal-only meetings.

## Operator-pinned rules (banner inside the prompt)

> «WHISPER MISHEARS THINGS — THIS IS THE MAIN JOB.»
> Worked examples pin: «teaser → Tether», «Адног → ADNOC»,
> «Голдман Сакс → Goldman Sachs». Anti-examples: «Felix» person
> ≠ «Felix Capital»; «teaser deck» industry term ≠ Tether.

## Status

The active counterparty matching path is the two-pass
extract→resolve flow (Pass 1 + Pass 2). This single-call matcher
is invoked when a caller wants the simpler one-shot API; the
pipeline (`_step_match_counterparties`) uses Pass 1 + Pass 2.
