# Prompt: brief_extract_event_counterparties (FR-CR-05-168 stage 0)

## Purpose
Из Calendar event (title + description + attendees) выделить ВНЕШНЕГО организацию и список внешних людей. Internal attendees (@thehumanoid.ai) на стороне Python обрезаются до вызова.

## Input variables

```json
{
  "title": "SDF <> Humanoid | Intro call",
  "description": "Intro to Samer Nawaf Zawaideh, CIO of SDF.",
  "attendees_external_only": [
    {"email": "samer.zawaideh@sdf.ae", "displayName": "Samer Zawaideh"}
  ]
}
```

## Output JSON schema

```json
{
  "org_name": "Strategic Development Fund",
  "initial_persons": [
    {"person_name": "Samer Nawaf Zawaideh", "person_role": "CIO"}
  ]
}
```

## System prompt

`app/counterparty_briefs/extract.py::_EXTRACT_SYSTEM_PROMPT` —
сжатая operator-friendly формулировка. Любые правки prompt'a
делаются ТАМ; этот MD — описание интерфейса для документации.

## Validation

- `org_name` либо string, либо null.
- `initial_persons` — list of objects с `person_name` (required).

## Eval cases

1. Title `"SDF <> Humanoid"` + attendee `samer.zawaideh@sdf.ae` →
   `org_name="Strategic Development Fund"`, persons включают Samer.
2. Title `"Артем-Алина sync"` + только internal attendees →
   `{org_name: null, initial_persons: []}`.
3. Title `"EQT Group <> Humanoid | Intro call"` без description →
   `org_name="EQT Group"`, persons=[].
