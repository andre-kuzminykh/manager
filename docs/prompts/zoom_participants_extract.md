# Prompt — `PARTICIPANTS_EXTRACT_SYSTEM`

> FR-CR-05-130 — for Zoom meetings, extract participant
> real_names from the transcript, validated against the
> Team sheet, dropping hallucinations.

| Field | Value |
|---|---|
| Source | [`app/services/zoom_participants.py:25`](../../app/services/zoom_participants.py) |
| Constant | `PARTICIPANTS_EXTRACT_SYSTEM` |
| Caller | `extract_zoom_participants_via_llm(...)` |
| Wired in | `_step_short_summary` (Zoom only) |
| LLM mode | `complete_text(response_format={"type": "json_object"})` |

## Why a separate prompt

Zoom doesn't reliably ship a participants list with the API
payload — the operator wants the «Участники: …» line in the
short summary to come from team_members.real_name matches inside
the transcript, NOT from Whisper-misheard names that don't
exist on the team.

## Input

User prompt holds:
- The transcript.
- A `team_members` table: every active row with `real_name`,
  `role`, `notes`.

## Output contract

```json
{ "participants": ["<real_name>", ...] }
```

- Names MUST come from the `team_members.real_name` set.
- Drop any name not in the set (hallucination guard).
- Empty `[]` is valid (one-on-one external meeting where no
  Humanoid teammate is named in the transcript).

## Operator-pinned rules

1. Match phonetic Whisper variants («Алинна» → «Алина») to the
   canonical real_name from the sheet.
2. Don't include external counterparties (those go through the
   counterparty matcher).
3. Order by first mention in the transcript.

## Fallback

Caller falls back to `row.participants` (whatever Zoom API
returned) if the LLM call fails or returns empty.
