# Note Taker — User Stories

Формат: `Как [роль] я хочу [действие], чтобы [бенефит]`. Каждая US имеет ID и список покрывающих UC + FR.

---

## US-NT-1 — Авто-захват Zoom встречи

> **Как** Артём
> **я хочу**, чтобы любая моя облачная Zoom-запись после завершения автоматически попадала в систему
> **чтобы** мне не нужно было руками ничего загружать.

**User flow:**
1. Артём проводит Zoom встречу с включённой Cloud Recording.
2. Zoom процессит запись (5-15 мин после конца).
3. Listener в течение ≤60s после готовности обнаруживает её через `/accounts/me/recordings`.
4. Pipeline автоматически: download → transcribe → ... → publish.

**Покрывают:** UC-NT-01, FR-NT-1.1, FR-NT-1.2, FR-NT-2.1
**Tested by:** T-NT-001, T-NT-002

---

## US-NT-2 — Авто-захват Fireflies встречи

> **Как** Артём
> **я хочу**, чтобы Fireflies-встречи (через ассистента Fred) после готовности транскрипта автоматически попадали в систему
> **чтобы** не дублировать ручную выгрузку.

**User flow:**
1. Fred (Fireflies) присоединяется к встрече, записывает.
2. Fireflies процессит и выкатывает transcript (5-30 мин).
3. Listener через GraphQL обнаруживает новый transcript в течение ≤60s.
4. Pipeline: download (если audio_url) или skip-transcribe (если текст уже есть) → ... → publish.

**Покрывают:** UC-NT-02, FR-NT-1.3, FR-NT-1.4
**Tested by:** T-NT-003, T-NT-004

---

## US-NT-3 — Получение карточки в Slack

> **Как** Артём (или коллега)
> **я хочу** получать в Slack DM компактную карточку (≤3 KB) с заголовком, участниками, сутью и списком задач
> **чтобы** быстро узнавать что было на встрече, не открывая полный отчёт.

**User flow:**
1. После завершения pipeline — pipeline постит short_summary в Slack channel `D0ASY5QF6UX` (Артём DM).
2. Длинный summary (>3500 chars) — первый chunk в канале + остальное в треде.
3. Первая строка карточки — clickable HTML link на Google Doc (`<doc_url|DD/MM - title>`).

**Покрывают:** UC-NT-05, FR-NT-9.1, FR-NT-9.2, FR-NT-9.3, NFR-NT-U.1
**Tested by:** T-NT-010, T-NT-011

---

## US-NT-4 — Получение полного отчёта в Google Doc

> **Как** Артём (или участник встречи)
> **я хочу**, чтобы был доступен полный structured отчёт (~20-30 KB) встречи в Google Drive
> **чтобы** возвращаться к деталям через несколько недель/месяцев.

**User flow:**
1. После detailed_summary шага — pipeline создаёт Google Doc в Shared Drive folder.
2. Doc содержит markdown-структурированный отчёт (контекст, решения, действия, контрагенты).
3. URL doc'a включается в Slack/TG карточку как clickable заголовок.

**Покрывают:** UC-NT-05, FR-NT-9.4, FR-NT-5.1
**Tested by:** T-NT-012

---

## US-NT-5 — Получение JSON через webhook

> **Как** внешняя система (n8n)
> **я хочу** получать JSON-payload каждой опубликованной встречи на свой webhook
> **чтобы** автоматически перекидывать данные в Notion / Airtable / другую систему.

**User flow:**
1. После Slack-mirror шага — pipeline POST'ит JSON на `MEETING_WEBHOOK_URL`.
2. Payload содержит source/title/summary/participants/tasks_count/google_doc_url.
3. Failures (non-2xx) логируются, но не retry'ятся (fire-and-forget).

**Покрывают:** UC-NT-05, FR-NT-9.5, NFR-NT-I.1
**Tested by:** T-NT-013, T-NT-014

---

## US-NT-6 — Защита от мусорных summary

> **Как** Артём
> **я хочу**, чтобы встречи с битым/пустым/галлюцинированным транскриптом НЕ публиковались никуда
> **чтобы** не получать в Slack «содержательная часть отсутствует» каждое утро.

**User flow:**
1. Pipeline transcribe Zoom/Fireflies встречу.
2. Quality gate L3 (`is_transcript_unsummarizable`) — если <800 chars или ≥2 субтитровых маркера → mark done, skip всё дальше.
3. Quality gate L5 (`is_summary_no_content`) — если LLM вернул фразы «содержательная часть отсутствует» → mark done, skip Slack/TG/webhook.
4. Запись остаётся в БД (для аналитики), но никуда не публикуется.

**Покрывают:** UC-NT-03, FR-NT-3.1, FR-NT-3.2, FR-NT-3.3, FR-NT-3.4, FR-NT-3.5
**Tested by:** T-NT-015, T-NT-016, T-NT-017

---

## US-NT-7 — Корректные имена встреч

> **Как** Артём
> **я хочу**, чтобы заголовок встречи всегда содержал дату и читабельное название (вместо «May 06, 02:33 PM» от Fireflies)
> **чтобы** ориентироваться по списку встреч.

**User flow:**
1. Pipeline проверяет `_looks_like_auto_stamp_title(row.title)`.
2. Если auto-stamp — сначала пытается через `match_calendar_title` (Google Calendar event ±30 мин окно).
3. Если no_match — derive через LLM из транскрипта.
4. Финал: первая строка summary = `DD/MM - <title>`.
5. (Fireflies only) push back в UI через `updateMeetingTitle` мутацию.

**Покрывают:** UC-NT-04, FR-NT-6.1, FR-NT-6.2, FR-NT-6.3
**Tested by:** T-NT-018, T-NT-019

---

## US-NT-8 — Распознавание участников

> **Как** Артём
> **я хочу**, чтобы в карточке были имена участников из team_members (а не email вроде «1@thehumanoid.ai»)
> **чтобы** сразу понимать кто был.

**User flow:**
1. Pipeline после transcribe запускает LLM `extract_zoom_participants_via_llm` с team_members справочником.
2. LLM возвращает list canonical real_names.
3. External email-участники (не из команды) — добавляются как есть.
4. Финальный list попадает в `row.participants` и используется в short_summary.

**Покрывают:** UC-NT-01 + UC-NT-02, FR-NT-4.1, FR-NT-4.2
**Tested by:** T-NT-020

---

## US-NT-9 — Контрагенты на встрече

> **Как** Артём
> **я хочу** видеть в БД (и в task descriptions) канонические названия контрагентов (Bosch, Stellantis, Schaeffler) даже если в транскрипте они написаны с искажением
> **чтобы** без проблем искать по компании поперёк всех встреч.

**User flow:**
1. Pipeline извлекает `mentions` из транскрипта через LLM.
2. Resolve в `counterparties` table (5 параллельных батчей × 20 mentions).
3. Unresolved mentions → enroll widget admin'у для ручного добавления.

**Покрывают:** UC-NT-01 + UC-NT-02, FR-NT-7.1, FR-NT-7.2
**Tested by:** T-NT-021

---

## US-NT-10 — Загрузка файла вручную (TODO)

> **Как** Артём
> **я хочу** иметь возможность скинуть боту в Telegram аудио/видео-файл встречи (например, GMeet record я скачал с Drive)
> **чтобы** прогнать через тот же pipeline.

**Покрывают:** UC-NT-06 (TODO), FR-NT-1.5 (TODO)
**Status:** ⏳ Q2-2026

---

## US-NT-11 — Голосовая диктовка задач (TODO)

> **Как** Артём
> **я хочу** надиктовать в TG voice-note список задач (например, перед сном)
> **чтобы** утром они уже были разнесены по людям и срокам.

**User flow (planned):**
1. Артём шлёт voice-note боту в TG.
2. Listener прокидывает audio в Whisper API.
3. Получает transcript → прогоняет через Note Taker pipeline (treat as «meeting» с одним participant = Артём).
4. Извлечённые tasks попадают в Task Tracker → DM owner'ам.

**Покрывают:** UC-NT-07 (TODO), FR-NT-1.6 (TODO)
**Status:** ⏳ Q2-2026

---

## US-NT-12 — Догон после downtime

> **Как** Артём
> **я хочу**, чтобы при перезагрузке/падении сервера пропущенные за период встречи догонялись автоматически
> **чтобы** ничего не теряли.

**User flow:**
1. Сервер падает / restart.
2. Listener стартует, делает первый poll Zoom (last 50 recordings) + Fireflies.
3. Все zoom_id/fireflies_id, которых нет в БД → процесс с нуля.
4. Все orphan'ы (`tasks_extracted=false OR last_error IS NOT NULL`) → retry idempotent.

**Покрывают:** UC-NT-08, FR-NT-10.1, FR-NT-10.2, NFR-NT-R.1
**Tested by:** T-NT-022

---

## US-NT-13 — DB-доступ для аналитики

> **Как** Виктор (BI)
> **я хочу** запрашивать опубликованные встречи через psql / SQL клиент по read-only доступу
> **чтобы** строить аналитику без нагружения мейнстрим-БД.

**User flow:**
1. Подключение через `pg-proxy:5433` с user `zoom_colleague`, role read-only.
2. SELECT из view `meeting_summaries_published` (фильтр по `short_summary IS NOT NULL`).
3. Фильтр по source: `WHERE source='zoom'` или `WHERE source='fireflies'`.

**Покрывают:** UC-NT-09, FR-NT-9.6, NFR-NT-S.1
**Tested by:** T-NT-023
