# Requirements ID Convention

## ID структура

```
FR-<AGENT>-<CATEGORY>.<SUB>
NFR-<AGENT>-<CATEGORY>.<SUB>
T-<AGENT>-<NUMBER>
US-<AGENT>-<NUMBER>
UC-<AGENT>-<NUMBER>
```

## Agents

- `NT` — Note Taker
- `TT` — Task Tracker
- `SH` — Shared (infra, data, общие компоненты)

## Categories для FR

### Note Taker
| Code | Категория |
|---|---|
| `1` | Source ingestion (Zoom / Fireflies / GMeet / manual / dictation) |
| `2` | Transcription (Whisper, fallbacks) |
| `3` | Quality gates (hallucination, thin transcript, no-content) |
| `4` | Participants resolution |
| `5` | Summarization (detailed + short) |
| `6` | Calendar match + title canonicalization |
| `7` | Counterparty matching |
| `8` | Task extraction (handoff to Task Tracker) |
| `9` | Distribution (Slack, TG, webhook, Google Doc) |
| `10` | Persistence (DB, idempotency) |

### Task Tracker
| Code | Категория |
|---|---|
| `1` | Source ingestion (TG, Slack, Email, Note Taker, Manual, Recurring) |
| `2` | Classification (intent: task/chitchat/question/status) |
| `3` | Drafts pipeline (title/desc/owner/date/priority) |
| `4` | Dedup |
| `5` | Persistence (Tasks table) |
| `6` | Distribution (TG cards) |
| `7` | Card lifecycle (statuses, buttons) |
| `8` | Digests (morning, evening) |
| `9` | Reminders (deadline, overdue) |
| `10` | Recurring tasks |
| `11` | Google Tasks two-way sync |
| `12` | Subscriptions |
| `13` | Audit log |

## Categories для NFR

| Code | Категория |
|---|---|
| `P` | Performance / latency |
| `R` | Reliability / availability |
| `S` | Security / privacy |
| `O` | Observability / monitoring |
| `C` | Cost (LLM tokens, API quotas) |
| `M` | Maintainability / code quality |
| `U` | Usability (UX) |
| `I` | Integration / compatibility |
| `D` | Data integrity / consistency |

ID example: `NFR-NT-P.1` = Note Taker Performance requirement #1.

## Naming use case files

```
UC-NT-NN_<short_slug>.feature
```

Where `NN` = двузначный инкремент (`01`, `02`, ...). `short_slug` = lowercase, snake_case.

## Naming tests

`T-NT-NNN` = тест #NNN для Note Taker. В каталоге `07_TESTS_TRACEABILITY.md` — таблица `T-XX-N → covers FR/NFR/UC`.

## Linking

Любой artifact ссылается на ID других через bracket-link:
> «Implements **FR-NT-1.1** + **FR-NT-1.2**, requirements from **US-NT-3**, tested by **T-NT-12**.»

Все ссылки backwards-traceable.
