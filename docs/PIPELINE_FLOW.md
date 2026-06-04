# Meeting pipeline — полный поток (актуально на 2026-06-04)

Как сейчас работает обработка встреч от записи до Slack/вебхука/задач, со всеми
включёнными фичами: нативные транскрипты, transcript-first FR-резолвер имён (3
трека), контент-гейт, FF-переименование.

Процесс: `ops/zoom_fireflies_runner` (контейнер `manager-zoom-ff-1`), два потока
— Zoom и Fireflies. Логика шагов: `app/zoom/pipeline.py`, `app/fireflies/pipeline.py`.

---

## 1. Топология и поллинг (ingest)

```mermaid
flowchart TD
  subgraph RUNNER["ops.zoom_fireflies_runner (1 процесс, 2 потока)"]
    ZP["_poll_zoom (interval 60s)"]
    FP["_poll_fireflies (interval 60s)"]
  end
  ZC["Zoom Cloud API"] -->|list_recordings| ZP
  FC["Fireflies API"] -->|list_transcripts| FP
  ZP --> G{"гейты ingest"}
  FP --> G
  G -->|"meeting_date >= now-LOOKBACK (24h)"| G2["время"]
  G2 -->|"участвует OPERATOR_REQUIRED_EMAIL"| G3["оператор"]
  G3 -->|"dur >= MIN_MEETING_SECONDS (300)"| G4["длительность"]
  G4 -->|"не дубль Zoom<->FF (first-to-post wins)"| G5["кросс-дедуп"]
  G5 -->|"не tasks_extracted в БД"| PO["process_one(meeting)"]
  G -.->|"любой гейт не прошёл"| SKIP["skip (лог, без обработки)"]
```

**Детали гейтов:**
- время: `started_at = now − OPERATOR_INGEST_LOOKBACK_HOURS(24)` — перезапуск не теряет день, старое не переобрабатывает;
- оператор: только встречи с `OPERATOR_REQUIRED_EMAIL` (`1@thehumanoid.ai`);
- длительность: `MIN_MEETING_SECONDS=300` (короче — `skipped_duration_too_short`);
- кросс-дедуп: одна встреча часто пишется и Zoom, и FF → `find_cross_source_duplicate`, кто первый запостил, тот выиграл;
- БД-дедуп: `tasks_extracted=true && last_error IS NULL` → `skipped_done` (идемпотентность).

---

## 2. process_one — конвейер шагов (идемпотентный)

```mermaid
flowchart TD
  S0["upsert строки (zoom_recordings / meeting_recordings)"] --> S1
  S1["1. download_audio"] --> S2
  S2["2. transcribe (см. §3)"] --> S3
  S3["3. detailed_summary (см. §4) — transcript-first FR-резолв"] --> S4
  S4["4. match/enroll counterparties (старый vector-резолвер)"] --> S5
  S5["5. extract_tasks (LLM) -> Task rows"] --> S6
  S6["6. verify_tasks (2-й проход)"] --> S7
  S7["7. canonicalize_task_names (наследует карту detailed)"] --> S8
  S8["8. consolidate / dedupe tasks"] --> S9
  S9["9. classify_directions"] --> S10
  S10["10. doc_export -> Google Doc (row.google_doc_url)"] --> S11
  S11{"11. short_summary + КОНТЕНТ-ГЕЙТ (см. §5)"}
  S11 -->|"пустая встреча"| STOP["return False -> НЕ постит НИКУДА"]
  S11 -->|"есть контент"| PUB["публикация (см. §6)"]
```

Каждый шаг идемпотентен (флаг на строке БД), при падении пишет `last_error` и
инкрементит `attempts`; кап `MAX_ATTEMPTS_BEFORE_GIVE_UP`.

---

## 3. Шаг transcribe — нативный транскрипт сначала (FR-NT-TR)

```mermaid
flowchart TD
  A["row.audio_path"] --> Z{"ZOOM_PREFER_NATIVE_TRANSCRIPT?"}
  Z -->|"да"| V["fetch Zoom VTT (_find_vtt_download_url + fetch_vtt_transcript)"]
  V --> SUN{"should_use_native?<br/>(>= NATIVE_TRANSCRIPT_MIN_CHARS 100)"}
  SUN -->|"да"| NAT["transcript = native_vtt<br/>(Whisper НЕ запускается)"]
  SUN -->|"нет / пусто"| WSP
  Z -->|"нет"| WSP["Whisper-путь (bias-prompt, чанки,<br/>VTT-fallback-on-hallucination, alt-model)"]
  NAT --> BIL
  WSP --> BIL["Zoom: билингва (детектор RU/EN -> англ. STT-проход -> LLM-merge)"]
  BIL --> OUT["row.transcript_text"]
  FF["FF: FIREFLIES_PREFER_NATIVE_TRANSCRIPT? -><br/>fetch_transcript_text (sentences); иначе Whisper"] --> OUT
```

**Детали:** `transcript_source = native_vtt | native_ff | whisper` пишется в трейс.
Инвариант: нативного нет/короткий → полный Whisper-fallback, транскрипт всегда
получается. Билингва Zoom не зависит от источника primary.

---

## 4. Шаг detailed_summary — transcript-first резолв имён

```mermaid
flowchart TD
  T["row.transcript_text (сырой)"] --> FR["resolve_for_meeting(text=transcript) (см. §4a)"]
  FR -->|"fr_map {форма->каноника}"| IMP["transcript = canonicalize_text(transcript, fr_map)<br/>= УЛУЧШЕННЫЙ транскрипт"]
  FR -->|"shadow: fr_map = {} (только лог)"| IMP
  IMP --> DS["detailed_summary = LLM(улучшенный транскрипт)"]
  DS --> CN["canonicalize_summary_text (team + counterparty, старый)"]
  CN --> RE["re-apply fr_map к detailed (FR-формы побеждают Amazon.com/Amanda)<br/>+ merge в _detail_canon_map"]
  RE --> DONE["row.detailed_summary (канонический)"]
  DONE --> TASKS["задачи берут _detail_canon_map"]
  DONE --> SHORT["короткое строится ИЗ detailed"]
```

### 4a. FR-резолвер — 3 трека (resolve_for_meeting)

```mermaid
flowchart TD
  GATE{"ENTITY_FR_RESOLVER_ENABLED?"} -->|"нет"| EMPTY["return {} (ноль изменений)"]
  GATE -->|"да"| CAT["fetch_catalog: MCP Виктора humanoid_fr_search(limit 2000)<br/>-> 1422 lean-строки, кэш TTL 1ч"]
  CAT --> ASM["assemble_shard_texts: CRM + team_members в ОДИН срез (~40K ток.)"]
  ASM --> MAP["ОДИН проход gpt-5.5 high: транскрипт x (CRM + команда)"]
  MAP --> D1["Track 1 (company) + Track 3 (team) решения"]
  D1 -->|"неузнанные люди + орг-подсказки (распознанные компании)"| P1["Track 2 LLM#1:<br/>кто человек + орг как звучит (SDF/Jabal)"]
  P1 --> SR["humanoid_fr_search(орг) -> запись с communication_log"]
  SR --> P2["Track 2 LLM#2: достать ПОЛНОЕ имя из прозы"]
  P2 --> D2["person решения (Самир->Samer, Daniel Gutenberg)"]
  D1 --> BR["build_replacements (conf >= ENTITY_FR_MIN_CONFIDENCE 0.65,<br/>split вариантов / , ;)"]
  D2 --> BR
  BR --> SH{"SHADOW?"}
  SH -->|"да"| LOG["лог в entity_fr_decisions, fr_map = {}"]
  SH -->|"нет"| APP["fr_map -> применяется к транскрипту+саммари"]
  D1 --> REC["каждое решение -> entity_fr_decisions (kind company/person/team)"]
  D2 --> REC
```

**Правила матчинга (промпт `_SYSTEM`):** CRM авторитетен по написанию; кросс-язык
транслитерация; предпочитать самую полную форму (Accenture Ventures > Accenture);
консистентность (мирая=Альмирая=мирка -> один Mirae); при неоднозначности
предпочитать active/in-pipeline; не матчить по созвучию в короткий акроним-фонд
(SDF/SVC/XTX); человек != компания; лучше null, чем кривое.

---

## 5. Контент-гейт (FR-CR-05-157e) — пустые встречи

```mermaid
flowchart TD
  ST["short_summary (LLM, из detailed)"] --> CG{"is_contentless_meeting(title, summary)?"}
  CG -->|"no-content фраза ИЛИ title 'без содержимого / no content'"| BLOCK["return False ДО row.short_summary<br/>=> НЕ постит ни в TG, ни в Slack, ни в вебхук"]
  CG -->|"есть контент"| OK["row.short_summary = text -> публикация"]
```

Закрыл регресс «Запись без содержимого» (0 задач, 15К payload уходил и в Slack,
и в n8n). `summary_has_body` остаётся доп.гейтом на вебхуке.

---

## 6. Публикация (фан-аут)

```mermaid
flowchart TD
  SS["row.short_summary (канонический, прошёл контент-гейт)"] --> TG["Telegram DM оператору (чанки)"]
  SS --> SL["Slack-зеркало -> SLACK_MEETING_CHANNEL_ID"]
  SS --> WH{"summary_has_body?"}
  WH -->|"да"| N8N["n8n вебхук MEETING_WEBHOOK_URL<br/>(source, title, summaries, doc_url, participants, tasks_count)"]
  WH -->|"нет"| WSKIP["meeting_webhook_skipped_empty_body"]
  SS --> AUTO["авто-публикация -> AUTO_SEND_TO_SLACK_CHANNEL (D0ASY5QF6UX)"]
  TASKS["Task rows (канонические имена)"] --> CARDS["post_task_cards (по карточке на задачу, последним)"]
```

**FF-переименование:** при derived-title (`_derive_topic_title`, английский) или
календарном матче — пуш в Fireflies UI как `DD/MM - Title` (`update_transcript_title`).
Zoom не переименовываем (политика «только Fireflies»).

---

## Флаги (env), актуальные значения в проде

| Флаг | Прод | Что |
|---|---|---|
| `ZOOM_PREFER_NATIVE_TRANSCRIPT` / `FIREFLIES_PREFER_NATIVE_TRANSCRIPT` | true | нативный транскрипт сначала |
| `ZOOM_BILINGUAL_RESTORATION_ENABLED` | true | англ. проход + merge (Zoom) |
| `ENTITY_FR_RESOLVER_ENABLED` | true | FR-резолвер имён |
| `ENTITY_FR_RESOLVER_SHADOW` | true→false | shadow (лог) -> apply (подмена) |
| `ENTITY_FR_PEOPLE_ENABLED` | true | Track-2 (люди из comm_log) |
| `ENTITY_FR_MIN_CONFIDENCE` | 0.65 | порог применения |
| `ENTITY_FR_MCP_URL` | (MCP Виктора) | источник CRM |
| `MIN_MEETING_SECONDS` | 300 | мин. длительность |
| `MEETING_WEBHOOK_URL` / `AUTO_SEND_TO_SLACK_*` | set | доставка |

## Где смотреть в коде
- ingest/поллинг: `ops/zoom_fireflies_runner.py`
- шаги: `app/zoom/pipeline.py`, `app/fireflies/pipeline.py`
- транскрипт: `app/services/transcription.py` (`should_use_native`, `is_contentless_meeting`)
- FR-резолвер: `app/services/entity_resolver_fr.py`, `entity_people_fr.py`, `fr_resolve_step.py`
- вебхук/Slack: `app/services/meeting_webhook.py`, `app/services/slack_mirror.py`
