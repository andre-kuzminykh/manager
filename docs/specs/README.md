# Spec Catalog — Humanoid CEO Brain

Главный pointer на все спеки. Структура enterprise-grade: User Stories → Use Cases (BDD/Gherkin) → Functional/Non-Functional Requirements → Architecture (Mermaid) → AI Prompts → Delivery Plan → Tests с traceability.

## Два агента

### 1. Note Taker
Принимает meeting-записи (Zoom, Fireflies, GMeet, manual upload, voice dictation), производит structured «карточку встречи» и распространяет.

- [00_OVERVIEW](./note_taker/00_OVERVIEW.md)
- [01_USER_STORIES](./note_taker/01_USER_STORIES.md)
- [02_USE_CASES/](./note_taker/02_USE_CASES/)
- [03_REQUIREMENTS/](./note_taker/03_REQUIREMENTS/)
- [04_ARCHITECTURE/](./note_taker/04_ARCHITECTURE/)
- [05_AI_PROMPTS/](./note_taker/05_AI_PROMPTS/)
- [06_DELIVERY_PLAN](./note_taker/06_DELIVERY_PLAN.md)
- [07_TESTS_TRACEABILITY](./note_taker/07_TESTS_TRACEABILITY.md)

### 2. Task Tracker
Жизненный цикл задач: Telegram/Slack/Email/Note Taker → classify → assign → distribute (TG cards) → sync (Google Tasks).

- [00_OVERVIEW](./task_tracker/00_OVERVIEW.md)
- [01_USER_STORIES](./task_tracker/01_USER_STORIES.md)
- [02_USE_CASES/](./task_tracker/02_USE_CASES/)
- [03_REQUIREMENTS/](./task_tracker/03_REQUIREMENTS/)
- [04_ARCHITECTURE/](./task_tracker/04_ARCHITECTURE/)
- [05_AI_PROMPTS/](./task_tracker/05_AI_PROMPTS/)
- [06_DELIVERY_PLAN](./task_tracker/06_DELIVERY_PLAN.md)
- [07_TESTS_TRACEABILITY](./task_tracker/07_TESTS_TRACEABILITY.md)

## Shared (общее для обоих агентов)

- [INFRA](./shared/INFRA.md) — GCP, networking, secrets, backups, CI/CD
- [DATA_ER](./shared/DATA_ER.md) — Entity-Relationship diagram (Mermaid)
- [DATA_DFD](./shared/DATA_DFD.md) — Data Flow Diagrams per agent
- [REQUIREMENTS_GLOSSARY](./shared/REQUIREMENTS_GLOSSARY.md) — convention для FR/NFR ID

## ID conventions

- **US-NT-N** — User Story Note Taker, инкрементальный N
- **UC-NT-N** — Use Case Note Taker (имя файла = `UC-NT-NN_short_slug.feature`)
- **FR-NT-X.Y** — Functional Requirement Note Taker, X = категория, Y = подномер
- **NFR-NT-X.Y** — Non-Functional Requirement Note Taker
- **T-NT-N** — Test Note Taker, traceable до FR/NFR
- Аналогично для Task Tracker (`-TT-`).

## Workflow для разработки

1. Любой новый функционал → User Story в `01_USER_STORIES.md`
2. → Use Case в `02_USE_CASES/UC-XX-NN_*.feature` (Gherkin)
3. → FR/NFR с ID в `03_REQUIREMENTS/`
4. → если архитектурно нетривиально — обновить `04_ARCHITECTURE/`
5. → если новые LLM-prompts — добавить в `05_AI_PROMPTS/`
6. → Decompose в `06_DELIVERY_PLAN`: Feature → User Flow → Task → Subtask + AC
7. → TDD: написать tests из `07_TESTS_TRACEABILITY` (link к FR/NFR ID)
8. → код пишем чтобы tests прошли

Все spec changes — отдельный PR, ревью before code starts.
