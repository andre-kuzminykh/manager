# Prompt: brief_research_org (FR-CR-05-168 stage 1)

## Purpose
Deep research брифинг по компании. Используется `o4-mini-deep-research` (web search ON).

## Input variables

```json
{"org_name": "Strategic Development Fund"}
```

## Output JSON schema

```json
{
  "name": "Strategic Development Fund",
  "official_name": "Tawazun Strategic Development Fund (SDF)",
  "website": "https://www.sdf.ae",
  "headquarters": "Abu Dhabi, UAE",
  "type": "Sovereign Wealth Fund",
  "sector_focus": ["Defense", "Aerospace", "IT"],
  "leadership": [
    {"name": "Samer Nawaf Zawaideh", "role": "CIO",
     "linkedin_url": "...", "evidence_url": "..."}
  ],
  "portfolio_highlights": [{"name": "HiSky", "deal_size": "$30M", "year": "2021"}],
  "recent_news": [{"date": "2026-04-...", "title": "...", "url": "..."}],
  "overview_paragraph": "...150-250 words..."
}
```

## System prompt
`app/counterparty_briefs/research.py::_ORG_SYSTEM_PROMPT`.

## Validation
Any missing field → null / N/A. Bad-shape output → runner returns None.

## Notes
- `o4-mini-deep-research` сам выбирает источники.
- `evidence_url` для каждой leadership row помогает на ревью.
- Max 20 leadership rows; max 20 portfolio_highlights; max 10 recent_news.
