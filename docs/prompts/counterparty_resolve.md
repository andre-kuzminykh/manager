# Prompt — `COUNTERPARTY_RESOLVE_SYSTEM` (Pass 2)

> FR-CR-05-129 — map each Pass-1 mention to a directory
> `id` (or `null` when no plausible match exists).

| Field | Value |
|---|---|
| Source | [`app/services/counterparty_match.py:220`](../../app/services/counterparty_match.py) |
| Constant | `COUNTERPARTY_RESOLVE_SYSTEM` |
| Caller | `resolve_mentions_to_directory(...)` |
| Wired in | `_step_match_counterparties` (Fireflies + Zoom) |
| LLM mode | `complete_text(response_format={"type": "json_object"})` |

## Why a separate Pass-2

Phonetic + Cyrillic↔Latin matching is the whole job. The LLM
gets a clean numbered list of mentions + the trimmed directory
(shortlist after Python-side fuzzy prefilter, FR-CR-05-126) and
returns the canonical `directory.id` for each. Multiple
phonetic forms collapsing to the same id is by design.

## Input

User prompt holds:
- `mentions`: the verbatim list from Pass 1.
- `directory`: `id | name` table (FR-CR-05-132 — `type`
  removed). Shortlisted by `_shortlist_directory_for_transcript`
  to ≤1000 plausible candidates.

## Output contract

```json
{ "matches": [
  { "mention": "<verbatim>", "directory_id": <int|null> },
  ...
] }
```

- One entry per input mention, **same order**.
- Same directory id may repeat (multiple phonetic variants of
  one entity).
- `null` ONLY when no plausible match exists. The pipeline's
  `_step_enroll_unresolved` (FR-CR-05-133) picks these up and
  posts «Track this entity?» widgets to admin DMs.

## Operator-pinned rules

1. Never invent directory ids.
2. Map phonetic / Cyrillic-Latin variants to the same canonical
   entry («Тезер» / «teaser» → Tether; «Бауэрдарт» / «Bauer/Dart»
   → Bauerdart; «Голдман» / «Goldman» → Goldman Sachs;
   «Адног» / «АДНОК» → ADNOC).
3. Preserve the input order in the output.

## Downstream consumers

- Persisted as `CounterpartyMention(source_kind, source_id,
  counterparty_id)` rows.
- Unresolved `null` mentions stash on
  `row.__dict__["_*_unresolved_mentions"]` for FR-CR-05-133.
- `mention → canonical_name` dict feeds Pass 3 (canonicalize).
