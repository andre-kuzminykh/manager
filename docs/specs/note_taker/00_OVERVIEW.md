# Note Taker — Overview

## Mission

Принимать любую meeting-запись (видео/аудио/готовый транскрипт), производить структурированную «карточку встречи» (участники, итог, решения, действия) и распространять её в Slack, Telegram, Google Doc и через webhook (n8n).

## Кто пользователь

- **Артём (CEO)** — главный consumer карточек встреч
- **Команда (Ирина, Алина, Даня, и т.д.)** — owner'ы задач, извлечённых из встреч
- **External consumer (n8n)** — забирает payload через webhook для дальнейшей обработки (Notion, Airtable)
- **Виктор (BI)** — пишет SQL-аналитику через DB view

## Ключевые свойства

| Свойство | Описание |
|---|---|
| **Source-agnostic pipeline** | Один и тот же 18-step pipeline для Zoom/Fireflies/GMeet/manual; различается только download-step |
| **Idempotent steps** | Каждый шаг имеет флаг в БД, повторный run не дублирует и не платит за API заново |
| **Quality-first** | 5 уровней защиты от мусорных summaries — лучше промолчать, чем опубликовать «содержательная часть отсутствует» |
| **Multi-output** | Slack + TG + Google Doc + webhook + DB view — параллельно, независимо |
| **No-loss recovery** | Container reset / SIGKILL → next poll-tick подхватит как orphan, idempotent steps пропустят cached |

## Что делает / Что не делает

### ✅ Делает

- Polling Zoom/Fireflies каждые 60s
- Whisper transcription (с hallucination detection и fallback на VTT)
- Detailed summary (~10-30 KB) → Google Doc
- Short summary (~1-3 KB) → Slack post + TG DM
- Counterparty extraction + resolve через справочник
- Извлечение задач (handoff в Task Tracker через shared DB)
- Webhook POST на n8n
- Calendar match для Fireflies-встреч (rename title по событию календаря)

### ❌ Не делает

- Назначение task owner'ов / deadlines / priorities — это Task Tracker
- Status updates / digests / reminders — это Task Tracker
- Project management (Gantt, sprints) — out of scope
- Раздел Slack DM с reply кнопками — не нужно

## Текущее состояние реализации

| Источник | Status | Comment |
|---|---|---|
| Zoom | ✅ Production | Daily processing работает |
| Fireflies | ✅ Production | + calendar_match + push back в Fireflies UI |
| GMeet | ⏳ TODO Q2-2026 | Через Drive API |
| Manual upload | ⏳ TODO Q2-2026 | Через TG voice-note или web upload |
| Voice dictation | ⏳ TODO Q2-2026 | TG voice → live Whisper stream |

См. далее:
- [01_USER_STORIES](./01_USER_STORIES.md)
- [02_USE_CASES/](./02_USE_CASES/)
- [03_REQUIREMENTS/](./03_REQUIREMENTS/)
- [06_DELIVERY_PLAN](./06_DELIVERY_PLAN.md)
