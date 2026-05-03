# Prompt — `DATE_SYSTEM_PROMPT`

> Resolve a free-form Russian / English date phrase («завтра»,
> «до пятницы», «next Tuesday», «к концу недели») to an absolute
> ISO date.

| Field | Value |
|---|---|
| Source | [`app/intent/date_prompt.py:14`](../../app/intent/date_prompt.py) |
| Constant | `DATE_SYSTEM_PROMPT` |
| Caller | `app/intent/date_resolver.py` |
| LLM mode | Anthropic `call_tool` |

## Input

User prompt holds:
- The original message.
- The current `today` date (timezone-aware) so phrases like
  «завтра» / «через 3 дня» resolve correctly.

## Output

`{ "due_date": "<YYYY-MM-DD|null>" }`.

`null` when no date phrase exists or the phrase is too
ambiguous («скоро», «как-нибудь»).

## Operator-pinned rules

1. ISO format only.
2. Resolve relative phrases against `today`, not against the
   message timestamp (operator wants «sender's today»).
3. Russian + English natural-language phrases supported.
4. Time-of-day not parsed here — separate field
   (`due_time`, FR-CR-05-9).
