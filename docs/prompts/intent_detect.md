# Prompt — `DETECT_SYSTEM_PROMPT`

> Lightweight pre-classifier that decides if a Slack / TG
> message is even a TASK candidate before the heavier
> `SYSTEM_PROMPT` runs.

| Field | Value |
|---|---|
| Source | [`app/intent/detect_prompt.py:12`](../../app/intent/detect_prompt.py) |
| Constant | `DETECT_SYSTEM_PROMPT` |
| Caller | `app/intent/classifier.py` (early gate) |
| LLM mode | Anthropic `call_tool` (or pattern-matched short-circuit) |

## Why a separate detector

Most Slack / TG messages are NOT task candidates (chat,
reactions, status updates, links). Running the heavy
`SYSTEM_PROMPT` on every message wastes tokens. The detector
is cheap and short — it filters candidates so the heavy
prompt only fires on the ~15% of messages that look
actionable.

## Input

User prompt holds:
- The message text (no context window — kept tiny).

## Output

`{ "is_task_candidate": <bool>, "confidence": <float> }`.

When `is_task_candidate=false`, the pipeline emits `no_action`
without invoking the main classifier.

## Operator-pinned rules

1. False positives are cheap (next stage filters them); false
   negatives mean lost tasks — bias toward `true` when
   ambiguous.
2. Don't try to extract title / owner here — that's the next
   stage's job.
