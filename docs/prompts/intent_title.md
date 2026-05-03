# Prompt — `TITLE_SYSTEM_PROMPT`

> Generate a concise imperative title from a free-form Slack /
> TG message, when the upstream intent classifier flagged
> `create_task`.

| Field | Value |
|---|---|
| Source | [`app/intent/title_prompt.py:11`](../../app/intent/title_prompt.py) |
| Constant | `TITLE_SYSTEM_PROMPT` |
| Caller | `app/intent/classifier.py` (title field resolver) |
| LLM mode | Anthropic `call_tool` |

## Input

User prompt holds:
- The original message text.
- Optional context (thread, prior task titles for dedup).

## Output

`{ "title": "<imperative ≤80 chars>" }` via tool call.

## Operator-pinned rules

1. Imperative form, no «надо / нужно / сделать».
2. Drop honorifics; preserve names («подготовь отчёт Алине»
   → «Подготовить отчёт для Алины»).
3. **No third-party status promises** (FR-CR-05-13): «Алина сама
   отправит» is NOT a title — promote the action verb to the
   imperative («Дождаться отправки от Алины» or just skip).
4. ≤80 chars.

## Test pin

`test_intent_pipeline.py::test_title_prompt_forbids_third_party_status_promises`
pins «Нет Алина сама отправит» as a forbidden title with the
expected imperative rewrite.
