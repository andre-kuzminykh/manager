# Prompt — `COUNTERPARTY_EXTRACT_SYSTEM` (Pass 1)

> FR-CR-05-129 — list every counterparty mention surface form
> from a meeting transcript, verbatim. **No directory in the
> prompt** — that's Pass 2's job.

| Field | Value |
|---|---|
| Source | [`app/services/counterparty_match.py:143`](../../app/services/counterparty_match.py) |
| Constant | `COUNTERPARTY_EXTRACT_SYSTEM` |
| Caller | `extract_counterparty_mentions(...)` |
| Wired in | `_step_match_counterparties` (Fireflies + Zoom) |
| LLM mode | `complete_text(response_format={"type": "json_object"})` |
| Model | `FIREFLIES_TASKS_MODEL` (default `gpt-5.5-thinking`) |
| Reasoning | `FIREFLIES_TASKS_REASONING_EFFORT` (default `medium`) |

## Why a separate Pass-1

Whisper produces dozens of spelling variants of the same fund
in one transcript («Тезер» / «тезер» / «teaser» / «Tether»).
Splitting extraction from resolution lets the LLM list every
variant verbatim without forcing a directory match — Pass 2
then collapses variants to canonical ids.

## Input

User prompt holds:
- The full Whisper-transcribed meeting transcript (Russian +
  English speech, expect typos and phonetic errors).

## Output contract

```json
{ "mentions": ["<verbatim>", "<verbatim>", ...] }
```

- One entry per distinct surface form (case-sensitive, no
  normalisation).
- Empty list = valid for internal-only meetings.
- Phonetic / Cyrillic-Latin variants of the same brand each
  count as separate entries — Pass 2 deduplicates.

## Operator-pinned rules

1. List every distinct form, don't merge.
2. Skip generic words («инвестор», «фонд», «компания», «раунд»…)
   without a brand attached.
3. Skip first names of internal Humanoid speakers (they're
   participants, not counterparties).
4. Empty `{ "mentions": [] }` is allowed.

## Worked examples (in the prompt body)

Same transcript may yield: `«Тезер», «тезер», «teaser», «Tether»`
— four entries. `«Bauer/Dart», «BauerDart», «Баутерт», «Bower/Баутерт»`
— four entries.
