# Prompt: brief_extract_beneficiaries (FR-CR-05-168 stage 2)

## Purpose
Из `OrgResearch.leadership ∪ initial_persons (event) ∪ attendees (event)` выбрать до N ключевых бенефициаров.

## Input variables

```json
{
  "org_name": "Strategic Development Fund",
  "leadership": [{"name": "Samer Nawaf Zawaideh", "role": "CIO"}],
  "initial_persons": [{"person_name": "Samer Nawaf Zawaideh", "person_role": "CIO"}],
  "attendees": [{"email": "samer.zawaideh@sdf.ae"}],
  "max_n": 5
}
```

## Output JSON schema

```json
{
  "beneficiaries": [
    {"person_name": "Samer Nawaf Zawaideh",
     "person_role": "CIO",
     "evidence": "appears as attendee AND listed in leadership"}
  ]
}
```

## System prompt
`app/counterparty_briefs/extract.py::_BENEFICIARY_SYSTEM_PROMPT`.

## Bias
attendees + initial_persons > leadership-role (CEO/CIO/CFO/Board) > все остальные.
Дедуп по имени. Internal `@thehumanoid.ai` — НЕ включать.

## Eval cases

1. `attendees=[Samer]`, `leadership=[Samer (CIO), Khaled (CEO)]`,
   `max_n=2` → `[Samer, Khaled]` (Samer первым: он attendee).
2. `attendees=[]`, `leadership=10 строк`, `max_n=5` → top-5
   leadership (CEO / CIO / CFO / Founder / Board).
3. `attendees=[Внутренний @thehumanoid.ai]`, `leadership=[]` →
   `beneficiaries=[]`.
