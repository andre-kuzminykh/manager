# Prompt — `DETAILED_SUMMARY_SYSTEM`

> FR-CR-05-119 / FR-CR-05-129 — produce a long structured
> summary of a meeting transcript that's effectively a
> «structured transcript» — every topic, every decision,
> every owner-bound action item. Operator-pinned: 6000-15000
> chars, max info density.

| Field | Value |
|---|---|
| Source | [`app/fireflies/prompts.py:10`](../../app/fireflies/prompts.py) |
| Constant | `DETAILED_SUMMARY_SYSTEM` |
| Caller | `summarise_transcript_detailed(...)` |
| Wired in | `_step_detailed_summary` (Fireflies + Zoom mirror) |
| LLM mode | `call_tool(tool_name="record_detailed_summary", …)` |
| Model | `FIREFLIES_DETAILED_SUMMARY_MODEL` (default `gpt-5.5`) |

## Input

User prompt holds:
- `meeting_title`, `participants`, `meeting_date`.
- The full Whisper transcript.

## Output contract

Tool call returns `{ "detailed_summary": "<markdown>" }`.

Structure expected (operator-pinned):
- Section per topic / counterparty raised in the meeting.
- Per-section: context, what was decided, what's owned by whom,
  numbers / dates / amounts called out verbatim.
- 6000-15000 chars total — must NOT compress; this is the
  upstream source for the task-extraction prompt.

## Why so long

Operator regression: «нет никакого таргета по количеству, ты
извлекашь задачи из длинного саммари: надо длинное саммари
чтобы включало максимум информации». Short summary handles the
TG digest; detailed summary is for the Doc + the task extractor.

## Downstream consumers

- `_step_extract_tasks` reads `row.detailed_summary` (NOT the
  raw transcript) when building the task-extraction prompt.
- `_step_doc_export` writes the detailed summary verbatim into
  the Google Doc body.
- `_step_short_summary` re-prompts the LLM with the detailed
  summary as input (compresses → ≤4000 chars short summary).
