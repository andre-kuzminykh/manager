# Prompt — `intent.SYSTEM_PROMPT`

> Base spec — main intent extraction prompt for the Slack /
> Telegram ingestion path. Decides whether a chat message is a
> task, meeting, update, or no_action.

| Field | Value |
|---|---|
| Source | [`app/intent/prompts.py:7`](../../app/intent/prompts.py) |
| Constant | `SYSTEM_PROMPT` |
| Caller | `app/intent/classifier.py` |
| Wired in | Slack mention path (FR-1..5) + Telegram passive ingest (FR-CR-04-30) |
| LLM mode | Anthropic `call_tool` with prompt-caching breakpoint |

## Input

User prompt holds the Slack / Telegram source message + a
windowed context block (recent thread, prior tasks, etc.).

## Output intents

- `create_task` — message introduces a new actionable task.
- `create_meeting` — message proposes a new meeting.
- `update_task` — modifies an existing task by reference.
- `update_meeting` — modifies an existing meeting.
- `no_action` — chat / question / nothing to create.

## Operator-pinned rules (excerpt)

1. Be conservative — emit `no_action` with low confidence when
   ambiguous.
2. Prefer `create_task` over `update_task` when the message
   doesn't explicitly reference a pre-existing task.
3. Don't echo «надо» / «подготовь» as the title — extract the
   imperative.

## Sub-prompts

After this top-level intent classification, separate
specialised prompts handle each field:
- [`title`](./intent_title.md) — `TITLE_SYSTEM_PROMPT`
- [`owner`](./intent_owner.md) — `OWNER_SYSTEM_PROMPT`
- [`date`](./intent_date.md) — `DATE_SYSTEM_PROMPT`
- [`detect`](./intent_detect.md) — `DETECT_SYSTEM_PROMPT`

The split lets each prompt be cached separately + invoked only
when the field is missing.

## Caching

The system prompt is static — cached via Anthropic's
`cache_control` breakpoint (≤90% latency on subsequent calls
within the cache TTL).
