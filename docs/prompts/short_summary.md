# Prompt — `SHORT_SUMMARY_SYSTEM`

> FR-CR-05-127 — title-as-hyperlink digest for the operator's
> Telegram morning DM. Compress the detailed summary into one
> ≤4000-char message that fits Telegram's 4096-char hard cap.

| Field | Value |
|---|---|
| Source | [`app/fireflies/prompts.py:96`](../../app/fireflies/prompts.py) |
| Constant | `SHORT_SUMMARY_SYSTEM` |
| Caller | `summarise_transcript_short(...)` |
| Wired in | `_step_short_summary` (Fireflies + Zoom mirror) |
| LLM mode | `call_tool(tool_name="record_short_summary", …)` |

## Input

User prompt holds:
- `meeting_title`, `participants`, `meeting_date`.
- The DETAILED summary (NOT the raw transcript) — this prompt
  compresses, doesn't re-extract.

## Output contract

```
DD/MM - <Topic>

Участники: <comma-separated>

Суть:
<2-4 sentences>

To-Do:
1) <task title> (<owner>)
2) ...
```

Caller wraps line 1 in `<a href="<google_doc_url>">…</a>` after
the LLM finishes (`_wrap_short_summary_with_doc_link`). The
prompt is forbidden from emitting any «Подробный отчёт» / URL
trailer (FR-CR-05-127).

## Operator-pinned rules

1. New header «DD/MM - <Topic>» (FR-CR-05-120).
2. Single «Участники: …» line — no «Их сторона / Наша сторона»
   split.
3. Each To-Do item uses the canonicalize-Pass-3 description
   verbatim (no re-paraphrase).
4. Compact mode (FR-CR-05-128) — when the verbose body would
   exceed 4000 chars, the BUILDER (not LLM) drops the
   per-task descriptions and emits «N) Title (Owner)» only.
   Full descriptions still ship via the per-task DM cards.
