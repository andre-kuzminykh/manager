# BDD Specification — Feature → User Story → Use Case (Gherkin)

> Навигационная BDD-структура над плоскими 558× `FR-CR-*` из SPEC.md.
> Каждая Feature: User Story + User Flow + ключевые Use Cases (Gherkin) +
> диапазон FR-ID + тесты + ссылка на ARCH-якорь.
> **SPEC.md** = детальные требования; **этот файл** = BDD-карта;
> **TECHNICAL_ARCHITECTURE.md** = как реализовано.
> Образец расширенного use-case — ARCH §11.
>
> Полный построчный FR↔test mapping: [FR_TEST_MATRIX.md](FR_TEST_MATRIX.md).

---

## Карта фич

| # | Feature | FR-диапазон | Спека | ARCH |
|---|---|---|---|---|
| F1 | Passive task detection | Base, FR-CR-05-0x | SPEC.md | §6.3 |
| F2 | Conversational task creation (@mention) | CR-02, FR-CR-05-3x | SPEC.md | §2,§6.3 |
| F3 | Task lifecycle + owner + workload | CR-01 | SPEC.md | §4,§7 |
| F4 | Subscriptions + digests | CR-01, FR-CR-5/6 | SPEC.md | §4 |
| F5 | Google Sheets/Tasks sync | Base | SPEC.md | §7 |
| F6 | Note Taker (meeting→summary+tasks) | FR-CR-05-11x..16x | SPEC_NOTE_TAKER | §6.1,§6.2 |
| F7 | Entity Resolution (counterparty + V2) | FR-CR-05-125/191/193 | SPEC.md | §4,§5 |
| F8 | Telegram extraction | FR-CR-05-3x | SPEC_TASK_EXTRACTOR | §3,§6.3 |
| F9 | CEO Brain (Slack archive + Claude) | FR-CB2-* | SPEC_CEO_BRAIN_BOT | §5 |
| F10 | Counterparty Briefs | FR-CB-* | SPEC_COUNTERPARTY_BRIEFS | §5 |
| F11 | Meeting Agenda | FR-CR-05-164/166/192 | SPEC_MEETING_AGENDA | §4 |
| F12 | Slack auto-publish | FR-CR-05-194/198/199 | SPEC.md | §2,§4 |
| F13 | Pipeline reliability (retry/sentinel/units) | FR-CR-05-151/195/196/197 | SPEC.md | §6,§8 |

---

## F1 — Passive task detection

**User Story:** Как член команды, я хочу чтобы бот сам замечал actionable
сообщения в Slack/TG и предлагал задачу, чтобы ничего не терялось.

**User Flow:** message → intent classify → draft → DM-карточка → Confirm/Ignore

```gherkin
Feature: Passive task detection
  Scenario: Actionable message becomes a draft
    Given сообщение "@ivan подготовь отчёт к пятнице"
    When классификатор определяет intent=create_task
    Then создаётся action_draft state=proposed
      And owner резолвится из "@ivan", due из "пятнице"
  Scenario: Non-actionable message ignored
    Given сообщение "всем привет, как дела"
    When классификатор определяет intent=no_action
    Then draft НЕ создаётся
```
**FR:** Base passive detection · **Tests:** test_intent_pipeline.py,
test_classifier_error_paths.py · **ARCH §6.3**

---

## F2 — Conversational task creation

**User Story:** Как автор, я хочу @-упомянуть бота и догенерить
недостающие поля в треде, чтобы не заполнять форму.

**User Flow:** @mention → draft widget → бот спрашивает missing fields →
reply обновляет карточку (chat.update) → Confirm морфит виджет

```gherkin
Feature: Conversational task creation
  Scenario: Mention with missing fields
    Given @mention "сделай дизайн лендинга"
    When owner и due отсутствуют
    Then бот постит widget и спрашивает owner+due в треде
  Scenario: Reply fills field in place
    Given widget ожидает owner
    When автор отвечает "на Олю, к среде"
    Then карточка обновляется через chat.update (не новое сообщение)
  Scenario: Confirm morphs widget
    When нажата Confirm
    Then widget превращается в task-card, task → БД
```
**FR:** CR-02, FR-CR-05-34 (keyboard order) · **Tests:**
test_telegram_conversations.py, test_telegram_bot.py · **ARCH §2,§6.3**

---

## F3 — Task lifecycle + owner + workload

**User Story:** Как менеджер, я хочу полный жизненный цикл задачи
(Backlog→To Do→In Progress→Review→Done) с owner из allow-list и
дедлайном с учётом загрузки.

**User Flow:** create → assign owner (picker) → workload-aware due →
transitions → Done

```gherkin
Feature: Task lifecycle
  Scenario: Owner picker from allowed list
    Given ALLOWED_OWNERS = [Ivan, Olga]
    When создаётся task без owner
    Then показывается picker только с Ivan, Olga
  Scenario: Workload-aware deadline
    Given у Ivan 5 открытых задач
    When назначается новая
    Then предлагаемый due сдвигается с учётом загрузки
  Scenario: Status transition
    Given task в "To Do"
    When нажата "Start work"
    Then status → "In Progress", лог в task_status_history
```
**FR:** CR-01 · **Tests:** test_transitions, test_workload, test_owners ·
**ARCH §4,§7**

---

## F4 — Subscriptions + digests

**User Story:** Как заинтересованный, я хочу подписаться на задачу и
получать дайджесты (daily/weekly/deadline), чтобы быть в курсе.

```gherkin
Feature: Subscriptions and digests
  Scenario: Subscribe broadcasts updates
    Given пользователь подписан на task
    When task меняет статус
    Then подписчик получает broadcast
  Scenario: Daily digest
    Given наступило время дайджеста
    Then собираются задачи с дедлайнами + просрочки → сообщение
```
**FR:** CR-01, FR-CR-5/6 · **Tests:** test_digest, test_notifications,
test_subscriber_updates · **ARCH §4**

---

## F5 — Google Sheets/Tasks sync

**User Story:** Как оператор, я хочу чтобы confirmed-задачи зеркалились
в Google Sheets и Google Tasks.

```gherkin
Feature: Google sync
  Scenario: Confirmed task mirrors to Sheets
    Given task confirmed
    Then строка появляется в Google Sheet (google_sheets_sync)
  Scenario: Status change propagates
    When task → Done
    Then Google Tasks отмечает completed
```
**FR:** Base · **Tests:** test_google_sheets, test_google_tasks ·
**ARCH §7** · ⚠️ Google Tasks list 404 (техдолг §10)

---

## F6 — Note Taker (meeting → summary + tasks)

**User Story:** Как CEO, я хочу чтобы Zoom/Fireflies-встречи автоматически
превращались в detailed+short summary, Google Doc и список задач.

**User Flow (см. ARCH §6.1):** download → transcribe(Whisper) →
detailed_summary → counterparty match → extract_tasks → verify →
canonicalize → consolidate → doc_export → short_summary → cards

```gherkin
Feature: Note Taker
  Scenario: Zoom recording fully processed
    Given новая Zoom-запись ≥5 мин
    When zoom_fireflies_runner забирает её
    Then создаётся transcript, detailed+short summary, Google Doc, tasks
  Scenario: Bilingual restoration
    Given транскрипт пришёл одноязычным но звук двуязычный
    Then bilingual_restorer восстанавливает второй язык
  Scenario: Participants beat role-match
    Given на встрече только Дмитрий Седов (не Дроздов)
    When extract_tasks назначает fundraising-задачу
    Then owner = присутствующий, не absent role-match
```
**FR:** FR-CR-05-11x..16x, 139/142/145 · **Tests:** test_fireflies.py,
test_audio_transcription.py, test_team_members.py · **ARCH §6.1,§6.2**

---

## F7 — Entity Resolution (counterparty + V2)

**User Story:** Как оператор, я хочу canonical имена/компании в саммари и
задачи на реальных участников встречи.

**User Flow (V2, см. ARCH §5):** Step1 reasoning extract → Step2 matcher
(STRICT participants + notes disambiguation) → Step3 LLM-rewrite (падежи)
→ owner apply (DELEGATE)

```gherkin
Feature: Entity Resolution V2
  Scenario: Disambiguate identical first names via notes
    Given "Дима" в контексте fundraising outreach
      And notes Дроздова="ВСЕ ЧТО С ФОНДАМИ", Седова="КОНТРАКТЫ"
    When matcher резолвит
    Then выбор по notes-контексту
  Scenario: STRICT — only meeting participants
    Given raw_owner резолвится в не-участника встречи
    Then tm_real_name=null (Python scrub, FR-193b-6)
  Scenario: Collective pronoun → host
    Given raw_owner="мы" (CEO-level задача)
    Then owner = host/principal (FR-193b-8)
  Scenario: External owner → internal action fallback
    Given raw_owner="Arjen" (инвестор), action="отправить deck"
    Then owner = host (внутреннее действие), FR-193b-9
  Scenario: Email canonicalize
    Given participant "1@thehumanoid.ai"
      And TeamMember Артем с этим email
    Then резолвится в "Артем Соколов" (FR-199c)
  Scenario: Russian declension in rewrite
    Given "написать Лене"
    Then "написать Елене Радионовой" (LLM rewrite, не regex)
```
**FR:** FR-CR-05-125 (counterparty), 191 (summary canon), 193a-h (V2),
199c (email) · **Tests:** test_entity_matcher_people.py,
test_entity_apply.py, test_entity_rewrite.py, test_team_member_canonical.py,
test_reasoning_extract_prompt.py · **ARCH §4,§5**

---

## F8 — Telegram extraction

**User Story:** Как член команды, я пишу в TG-группу, и бот вычленяет
задачи в proposed-drafts мне в DM.

```gherkin
Feature: Telegram extraction
  Scenario: Group message → draft card
    Given сообщение в watched TG-чате с actionable текстом
    When listener_view_poll читает (seen=500)
    Then создаётся action_draft, DM-карточка с Confirm/Edit/Ignore
  Scenario: Inline LLM edit
    Given draft-карточка
    When автор отвечает "поменяй owner на Иру"
    Then parse_edit_with_llm обновляет draft
```
**FR:** FR-CR-05-3x, 35 (realtime poll) · **Tests:**
test_telegram_listener.py, test_telegram_conversations.py · **ARCH §3,§6.3**

---

## F9 — CEO Brain (Slack archive + Claude Q&A)

**User Story:** Как CEO, я хочу спросить бота в Slack про что угодно, и он
отвечает на базе архива сообщений + MCP-инструментов (calendar/tasks/web).

```gherkin
Feature: CEO Brain
  Scenario: DM question answered via Claude + MCP
    Given DM боту "что у меня по Tether на этой неделе?"
    When responder собирает контекст (parallel_gather 6 MCP)
    Then Claude отвечает, claude_responder_runs логирует cost/tokens
  Scenario: Archive-only mode
    Given CEO_BRAIN_ARCHIVE_ONLY=true
    Then сообщения архивируются, ответов нет
```
**FR:** FR-CB2-* · **Tests:** test_ceo_brain*.py · **ARCH §5** ·
Anthropic claude-sonnet-4-6 (отдельный ключ)

---

## F10 — Counterparty Briefs

**User Story:** Как CEO, перед встречей с контрагентом я хочу авто-бриф
(организация + люди) с web-research.

```gherkin
Feature: Counterparty Briefs
  Scenario: Brief generated ahead of calendar meeting
    Given Calendar-событие с внешним участником через N дней
    When brief runner срабатывает
    Then extract beneficiaries → research org+person (web) → Slack-бриф
  Scenario: Budget cap respected
    Given COUNTERPARTY_BRIEFS_LLM_BUDGET_USD=3.0
    Then research останавливается при достижении лимита
```
**FR:** FR-CB-* · **Tests:** test_counterparty_brief*.py · **ARCH §5** ·
o4-mini-deep-research (web search)

---

## F11 — Meeting Agenda

**User Story:** Как участник, перед встречей я хочу авто-агенду на базе
прошлых встреч + open tasks.

```gherkin
Feature: Meeting Agenda
  Scenario: Agenda from prior meetings
    Given Calendar-событие с ≥2 прошлыми встречами той же серии
    When agenda runner за lead_time до начала
    Then собирается агенда (open_tasks из last prior) → Slack
  Scenario: Drop resource attendees
    Given attendees содержат "Humanoid Office - London" (переговорка)
    Then ресурс исключается из участников (FR-192ac)
```
**FR:** FR-CR-05-164/166, 192aa/ab/ac · **Tests:** test_agenda*.py ·
**ARCH §4**

---

## F12 — Slack auto-publish

**User Story:** Как оператор, я хочу чтобы summary+важные задачи
автоматически уходили в Slack-канал в правильном формате.

```gherkin
Feature: Slack auto-publish
  Scenario: Parent + thread format
    Given обработанная встреча с short_summary
    When AUTO_SEND_TO_SLACK_ENABLED=true
    Then parent = "DD/MM - Title"(hyperlink) + Участники + суть + TODO trailer
      And thread reply = важные задачи (DIRECTIONS_IMPORTANT)
  Scenario: Idempotent (no double post)
    Given slack_post_ts уже установлен
    Then повторная публикация пропускается
  Scenario: V2 publish read-only
    Given ops/v2_publish_meeting --read-only
    Then Slack постится, БД не трогается
```
**FR:** FR-CR-05-194 (auto-publish), 198 (CLI), 199/199b/199c (format) ·
**Tests:** test_slack_publish_all_tasks.py · **ARCH §2,§4**

---

## F13 — Pipeline reliability

**User Story:** Как оператор, я хочу чтобы пайплайн само-восстанавливался,
не зацикливался на битых записях и правильно интерпретировал длительность.

```gherkin
Feature: Pipeline reliability
  Scenario: Self-heal unfinished recordings
    Given запись transcribed=t но tasks_extracted=f
    When listener poll
    Then process_one ретраит (FR-151), идемпотентно
  Scenario: Retry cap
    Given attempts >= 20
    Then skip permanent_failure (FR-196), без бесконечного retry
  Scenario: Zoom 24h sentinel
    Given duration_seconds == 86400
    Then skip (FR-197, Zoom Phone artifact)
  Scenario: Fireflies duration units
    Given Fireflies API duration=50 (минут)
    Then хранится 3000 сек (×60, FR-195), не скипается как <5мин
```
**FR:** FR-CR-05-151/195/196/197 · **Tests:**
test_fireflies_duration_units.py, test_retry_cap_and_sentinels.py ·
**ARCH §6,§8**

---

## Покрытие

13 фич агрегируют ~246 FR-ID. Детальные acceptance-критерии — в SPEC.md
по каждому FR. Точный FR↔test — [FR_TEST_MATRIX.md](FR_TEST_MATRIX.md).
Расширение: каждая Feature может добрать Gherkin-сценарии под edge-cases
из соответствующих FR (этот файл — навигационный костяк, не исчерпывающий).
