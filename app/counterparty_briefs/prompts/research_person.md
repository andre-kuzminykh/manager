# Prompt: brief_research_person (FR-CR-05-168 stage 3)

## Purpose
Deep research брифинг по конкретному физлицу. Используется `o4-mini-deep-research` (web search ON). Output матчит operator-pinned формат §6.2 (фото вверху + Personal Information + Profile Overview + Current/Previous Positions + Investment Highlights + Investments + Achievements + Honors + Education + Publications + Skills + Languages).

## Input variables

```json
{
  "person_name": "Samer Nawaf Zawaideh",
  "person_role": "CIO",
  "org_name": "Strategic Development Fund",
  "evidence": "appears as attendee AND listed in leadership"
}
```

## Output JSON schema

Схема в `app/counterparty_briefs/research.py::_PERSON_SYSTEM_PROMPT` (полный JSON со всеми операторскими полями).

## System prompt
`app/counterparty_briefs/research.py::_PERSON_SYSTEM_PROMPT`.

## Validation
- Photo URL — public только (LinkedIn авторизованный URL → null, его Google Docs не embed'нет).
- Любая пустая секция → null/N/A на стороне renderer'а.
- Bad shape → runner skips person; в Slack DM строка «👤 Name — N/A (research_failed)».

## Cost
1 call ≈ $0.5-1 (o4-mini-deep-research включая web search). Run only when:
- Person not in `counterparty_briefs` cache (TTL 180 days), AND
- Cumulative event cost spent + estimate ≤ `COUNTERPARTY_BRIEFS_LLM_BUDGET_USD` ($2 default).

## Eval cases

1. Известный investor (Samer Zawaideh) → photo + LinkedIn +
   полная investment-секция.
2. Малоизвестный physical person без LinkedIn → большинство полей
   N/A, но `personal_information.name` + `profile_overview`
   все равно заполняются.
3. Org-side service-account email → схема возвращается с пустым
   `personal_information.name` → caller (runner) использует
   beneficiary fallback name.
