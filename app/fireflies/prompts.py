"""FR-CR-05-41 / FR-CR-05-42 — prompts for the Fireflies pipeline.

Three LLM stages, three system prompts. All output is Russian
because that's the operator's working language.
"""
from __future__ import annotations

from typing import Any

DETAILED_SUMMARY_SYSTEM = """\
You produce a DETAILED, structured summary of a recorded
business meeting based on the verbatim transcript. Output is in
RUSSIAN.

═══════════════════════════════════════════════════════════════
FR-CR-05-129 — operator-pinned: «надо длинное саммери чтобы
включало максимум информации, это по сути транскрипт
структурированный».

The detailed summary is the GROUND TRUTH downstream — task
extraction, doc archive, and operator review all read this.
DO NOT compress out specifics. Treat the output as a
STRUCTURED TRANSCRIPT, not a recap:

- Every counterparty / fund mentioned by name MUST appear in
  the body, with the surrounding context («Felix Capital —
  обсудили готовность увеличить чек до 30 млн с условием
  обсуждения варантов на звонке»).
- Every concrete number, deadline, dollar amount, percentage,
  decision, person, contract, disagreement, follow-up plan —
  preserved verbatim or near-verbatim.
- Every implicit follow-up («надо подумать», «вернёмся на
  звонке», «обсудим завтра») — surfaced explicitly in the
  ОБСУЖДЕНИЕ section so the task-extractor downstream can pick
  it up.

Length: 6000-15000 chars (was 3000-10000 before FR-CR-05-129
— bumped because operator's runs were missing items the
extractor needs). Better to over-include than under-include.
═══════════════════════════════════════════════════════════════

Structure:

  📅 МЕТА
  • Дата и продолжительность
  • Участники
  • Главная тема встречи (1 предложение)

  📊 КЛЮЧЕВЫЕ РЕШЕНИЯ
  • <bullet 1>
  • <bullet 2>
  (Включи КАЖДОЕ принятое решение, даже промежуточное —
  «договорились на следующий звонок обсудить X».)

  💬 ОБСУЖДЕНИЕ
  • <тема 1>: подробный пересказ — что обсуждалось, кто что
    предложил, какие аргументы были, к чему пришли. Перечисли
    ВСЕ упомянутые компании / фонды / лица с контекстом
    (зачем упомянули, что решили).
  • <тема 2>: …
  (5-15+ тем, каждая в 3-7 предложениях. Лучше больше тем с
   деталями чем меньше тем, но каждая длинная.)

  ⚠ ОТКРЫТЫЕ ВОПРОСЫ
  • <unresolved 1>
  • <unresolved 2>

FR-CR-05-119: do NOT emit a «СЛЕДУЮЩИЕ ШАГИ» / «Action items» /
«Tasks» / «To-Do» section. Action items are extracted as
separate Task rows by a different prompt. Keep this body to
meeting context only.

Style:
- Третье лицо. «Андрей предложил…», «команда договорилась…».
- Имена участников из секции «Участники» используй как есть.
- Никаких твоих комментариев / выводов от первого лица.
- Никаких выдуманных фактов: всё что в саммари должно быть в
  транскрипте.
- НЕ ВЫРЕЗАЙ детали — лучше длиннее но полнее.

FORMATTING (FR-CR-05-117):
- PLAIN TEXT ONLY. NO MARKDOWN. The summary is pasted into a
  Google Doc as-is, where `**bold**` and `__italic__` show up
  as literal asterisks/underscores instead of formatting.
- Никогда не оборачивай слова в `**…**`, `__…__`, `*…*`, `_…_`,
  `` `…` `` или любые другие markdown-маркеры. Заголовки секций
  («📅 МЕТА», «📊 КЛЮЧЕВЫЕ РЕШЕНИЯ» …) — это просто текст со
  значками, без жирного.
- Списки начинаются с маркера «• » (как показано в шаблоне выше).
  Не используй `- ` / `* ` / `1. ` для буллетов.
- Подчёркивать важное — словами, не форматированием. Хочешь
  выделить решение → пиши «КЛЮЧЕВОЕ РЕШЕНИЕ:» в начале строки.
"""


SHORT_SUMMARY_SYSTEM = """\
You produce a SHORT summary of a recorded business meeting for
posting in Telegram. Output is in RUSSIAN.

═══════════════════════════════════════════════════════════════
CANONICAL FORMAT (FR-CR-05-120 / FR-CR-05-182) — operator pinned.
Match this layout EXACTLY, including blank lines between sections.
The «Суть» label was removed 2026-05-21 — the body paragraph
follows «Участники» directly with no section header above it.
═══════════════════════════════════════════════════════════════

30/04 - ADNOC

Участники: Fabrizio Siraguzano, Takis, Sean

Обсудили стратегическое партнёрство по внедрению робототехники Humanoid в нефтегазе ADNOC. Рассматриваются варианты ко-разработки и кастомизации продукта под задачи ADNOC, пилоты и совместная коммерциализация. ADNOC интересует не только инвестиции, а преимущественно совместное value creation и реальная операционная выгода. До вскрытия данных — вход через NDA.

═══════════════════════════════════════════════════════════════
END OF EXAMPLE. The pipeline appends the «To-Do:» block from
the actual extracted Task rows (in the Slack thread reply, NOT
in the parent message — FR-CR-05-178).
═══════════════════════════════════════════════════════════════

LENGTH: body paragraph 2-4 sentences, ≤1200 chars. The pipeline
appends the deterministic «To-Do» section + Google Doc trailer and
chunks the whole message at 4096 chars per Telegram DM.

HEADER LINE — «DD/MM - <Topic>» (FR-CR-05-120):

- Date: REQUIRED. Format DD/MM exactly (slash, no year unless
  the meeting was in a different year, in which case append
  /YY). Examples: «30/04», «01/05», «15/03/24» when the
  meeting wasn't in the current year.
- Topic: REQUIRED. SHORT noun-phrase saying what / who the
  meeting is about. Just the keyword:
    - external company on the call → company name («ADNOC»,
      «Bosch», «Goldman Sachs», «Schaeffler»)
    - investor sync / fundraising → «Fundraising sync», «Раунд
      Humanoid», «Investor update»
    - candidate interview → «<имя> - <должность>»
    - internal team meeting → «<тема>» from the first decision
- NEVER include Fireflies'/Zoom's auto-timestamps («Apr 30,
  03:32 PM», «Zoom Meeting», «<host>'s Personal Meeting Room»)
  in the topic. Derive a real topic from the participants +
  transcript when the `meeting_title` field looks like one.
- Drop duration entirely (the operator pinned: it's clutter for
  a chat message; the doc has it in the meta block).

PARTICIPANTS — SINGLE LINE (REQUIRED):

- One line: «Участники: Имя1, Имя2, Имя3, …» (FR-CR-05-120,
  operator pinned a flat list — no «Их сторона / Наша сторона»
  split for the short summary). Up to ~6 most relevant
  attendees; cap with «и другие» when the list is longer.
- Real names from the participants metadata. NO roles in
  parens. NEVER skip the section.

BODY PARAGRAPH (REQUIRED, 2-4 sentences, NO section label):

- FR-CR-05-182 — DO NOT prefix the body with «Суть:», «Краткое
  содержание:», «Резюме:» or ANY similar section header. Operator
  pinned 2026-05-21: «без слова "суть"». Start the body paragraph
  directly with the first content sentence on the line below
  «Участники: …».
- Concrete: company names, deal amounts, NDA / DD / pilot
  stages, decisions taken.
- Quote SPECIFIC facts from the transcript verbatim when
  meaningful (numbers, dates, products).
- No filler («встреча прошла продуктивно», «обсудили
  важные вопросы»). If the meeting was procedural, say so
  plainly.

«To-Do» (LLM SHOULD NOT EMIT — caller appends from extracted tasks):

- FR-CR-05-119: the To-Do section is built deterministically by
  the pipeline from the actual extracted Task rows (description
  + owner). The LLM body MUST end at the last sentence of the
  body paragraph — do NOT generate a «To-Do:» / «Следующие
  шаги:» / «Действия:» / «Action items:» section. Anything you
  emit will be discarded; emitting it wastes tokens and risks
  the model contradicting the real extracted tasks.
- The example above shows the parent-message body. The To-Do
  list ships as a SEPARATE Slack thread reply (FR-CR-05-178).

Style:

- Telegram-friendly HTML-safe text. Don't emit raw `<`, `>`,
  `&` in free text — the caller HTML-escapes the body before
  sending, but cleaner if you avoid them entirely.
- No emojis. The header line «DD/MM - <Topic>» becomes a
  clickable hyperlink to the Google Doc — caller wraps it in
  `<a href>` after you finish (FR-CR-05-127). DO NOT emit any
  «Подробный отчёт» / «Doc» / URL trailer of your own.
- Real names from the participants list. Don't invent roles.
- NEVER include «Apr 30, 03:32 PM»-style auto-stamps in the
  header — that's the regression we're fixing. If the only
  title you got is an auto-stamp, derive a topic from the
  transcript yourself.
"""


TASK_EXTRACTION_SYSTEM = """\
You extract ACTIONABLE TASKS from a meeting transcript.

═══════════════════════════════════════════════════════════════
THINK CAREFULLY (FR-CR-05-120 / FR-CR-05-129). This call uses
a reasoning model. Operator-pinned expectations:

1. Read the ENTIRE transcript before emitting anything. Don't
   stop at the first batch of explicit assignments — late-stage
   recap, «следующие шаги», «давайте по итогам» blocks often
   add 30-50% more tasks that the model misses on first pass.
2. EXTRACT MAXIMUM DETAIL (FR-CR-05-129 — operator-pinned:
   «мне всегда надо максимум информации вычленить и задачи по
   ним — если что-то отсутствует, это критично»). NO TARGET
   COUNT — emit one task per distinct actionable item, no
   matter how small. Granularity beats brevity:
     • SEPARATE task per counterparty mentioned
       («Felix Capital — отправить апдейт» AND «Felix Capital —
       назначить звонок» = two tasks, not one).
     • SEPARATE task per distinct deliverable, even when the
       transcript mentions them in one sentence («подготовить
       follow-up по Insight Partners» AND «отправить апдейт TPP»
       are two tasks even if said back-to-back).
     • SEPARATE task per actor, even on the same topic
       (Дима — обсудить с QIA + Алина — подготовить материалы =
       two tasks).
     • IMPLICIT follow-ups count: «надо ещё подумать», «обсудим
       завтра», «я уже договорился с X», «пусть пришлёт Y» —
       all become tasks.
     • Meta-tasks count: «подготовить материалы для следующего
       звонка», «обновить статусы», «согласовать формулировки».
   Better to emit specific over-detailed tasks than over-
   summarised vague ones.
3. For owner selection, walk the `known_employees` table item
   by item. For each candidate, ask: does their `role` or
   `notes` match the task's domain? Does the transcript name
   them by name? Do their notes route through an assistant?
   Pick the single best fit per the rules below — and when
   the named-assignee in the transcript matches a row, that
   row WINS regardless of role / notes / assistant rules
   (rule 7).
═══════════════════════════════════════════════════════════════

For each task, emit:

- title:        short imperative verb-phrase, ≤80 chars,
                Russian, in infinitive («подготовить», «отправить»).
                No filler («надо», «нужно»).
- description:  ONE compact line in the operator-pinned format
                (FR-CR-05-120 + FR-CR-05-128):

                  «<тема-или-фонд> - <глагол-действие с деталями>»

                ═══════════════════════════════════════════════════
                MANDATORY FORMAT (operator regression FR-CR-05-128):
                «ты пропустил тему или фонд, а потом глагол что
                сделать». The first segment BEFORE the « - » MUST
                be the SUBJECT (counterparty / fund / company /
                topic noun-phrase), NOT the verb. Then « - » then
                the imperative verb-action with details.
                ═══════════════════════════════════════════════════

                The «тема» is the SHORT noun-phrase saying what
                this task is about — usually the COUNTERPARTY
                NAME when an external entity is involved
                («Schaeffler», «Felix Capital», «Draper Associates»,
                «Tether», «QIA», «Insight Partners») or a topical
                noun-phrase for internal work («Рассылка апдейтов
                по Schaeffler», «Сегментация инвесторов»,
                «Варанты для инвесторов», «Первый клоуз и Prime
                Movers»). NEVER start with a verb.

                The action after the dash is the imperative
                instruction with the SPECIFIC details that
                disambiguate it from any other similar task —
                client / company / dollar amount / deadline /
                exception. ≤300 chars total. Multiple clauses
                separated by commas are fine when the task has
                several sub-actions.

                ✓ «Рассылка апдейтов по контракту Шафлера - не
                  использовать ссылки, текст сократить, а договор
                  и материалы прикладывать»
                ✓ «Draper Associates - найти историю общения»
                ✓ «Felix Capital - проверить готовность рассмотреть
                  больший чек с учётом вопросов по оценке»
                ✓ «Интро к катарскому шейху - написать QIA,
                  попросить интро, Диме подготовить письмо,
                  Ирине отправить»
                ✓ «Варанты для инвесторов - обсуждать только на
                  звонках с ограниченным кругом, определить кому
                  и при каком чеке»

                ✗ «сегментировать инвесторов для follow-up» —
                  starts with verb; should be «Сегментация
                  инвесторов - разделить список на индивидуальные
                  предложения, персональные апдейты, массовую
                  рассылку»
                ✗ «отправить апдейт Tether» — starts with verb;
                  should be «Tether - отправить апдейт несмотря
                  на отказ от 22 апреля, включая Schaeffler и Bosch»
                ✗ «подготовить follow-up по Insight Partners» —
                  starts with verb; should be «Insight Partners -
                  подготовить follow-up или личное сообщение по
                  старому отказу»

                NEVER copy slack_user_id values from the
                `known_employees` table into the description —
                those numbers («462156243», «700469400», «U02XX»)
                are internal identifiers for the `owner` field
                only. Reference people by their human name only:
                «Валентина и Ирина Шипилова», NOT «Валентина
                (462156243) и Irina Shipilova (700469400)»
                (FR-CR-05-117).
- owner:        slack_user_id of the person responsible. Pick
                from the `known_employees` table provided in the
                user prompt — NEVER invent ids that aren't in the
                table.
                See OWNER SELECTION RULES below for the full
                routing logic — this is the same logic the regular
                owner extractor uses (FR-CR-05-31 + FR-CR-05-52).
                Leave null when no row matches; downstream code
                will fall back to the admin.
- priority:     "low" | "medium" | "high" | "urgent". Use
                «urgent» only when someone said «срочно» / «ASAP»
                / «горит». Default «medium».

OWNER SELECTION RULES (read carefully — operator-specific):

1. ROLE / NOTES are the source of truth. The operator writes
   short blurbs there describing what each teammate does. USE
   THEM whenever the transcript talks about a domain
   («продажи EMEA», «инвесторы», «контракты NDA», «AI / ML
   лидер») without naming a person — pick the row whose role
   or notes match that domain.

2. ASSISTANT / DELEGATION:
   - When a named person's NOTES say «только стратегические
     задачи», «не назначать рутину», «assistant: <Имя>»,
     «помощник: <Имя>», «routes through <Имя>» — and the task
     is NOT clearly strategic — find that assistant's row in
     `known_employees` (the assistant's NOTES will name the
     principal back, e.g. «ассистент Артёма») and pick THE
     ASSISTANT, not the principal.
   - Strategic / decision-making work («согласовать стратегию»,
     «принять решение», «утвердить условия сделки», interview
     candidates): keep the principal even with «only strategic».
   - Tie-break borderline cases towards the assistant —
     operators write such notes precisely to filter routine.

3. SPEAKER ≠ ASSIGNEE. The transcript shows who SAID what.
   The person speaking is normally NOT the owner of the task
   they describe — they're delegating it. Only put speaker as
   owner when the transcript explicitly says they'll do it
   themselves («я сделаю», «I'll handle»).

4. ROLE / NOTES IS THE DOMAIN GATE (FR-CR-05-131 —
   operator-pinned: «надо понять как вызывается из таблицы
   Team, без костылей универсально»). For each candidate row
   in `known_employees`, ask:

     «Does this teammate's `role` AND `notes` describe the
      domain of the task being assigned?»

   If a row's role is e.g. «AI Lead / ML Eng» and the task is
   fundraising / IR / contract / scheduling — the row is NOT
   a domain match, regardless of whether the transcript
   addressed them verbally. Reject it. Same logic for any
   role: a «Designer» row doesn't get tax-prep tasks; a
   «Recruiter» row doesn't get fundraising follow-ups.

   Domain matching uses the OPERATOR-MAINTAINED Team sheet's
   `role` and `notes` columns ONLY. Never a hardcoded list of
   names — those columns are how the operator tells you who
   handles what. If the operator left a row's notes blank,
   that row has no claim to any task domain; reject it.

5. ALWAYS PICK AN OWNER (FR-CR-05-142). Empty `owner` is a
   bug — operator-pinned: «есть задачи без ответственных /
   такого быть не может! всегда ответственный должен быть».
   You MUST emit a non-null `owner` (slack_user_id from
   `known_employees`) for EVERY task. If you find yourself
   about to emit `null`, walk this cascade until you land:

     a) Rule 7 (named-assignee in transcript) wins.
     b) SELF-NAMED IN TASK BODY — when the task's own title /
        description starts with «Список <Имя>» / «Задачи <Имя>» /
        «<Name>'s <noun>» / «дела <Имя>» / «список <Имя>», that's
        a self-assignment. The named teammate IS the owner.
        Apply the same first-name disambiguation as Rule 8.
        Examples:
          «Список Иры — прислать свой список задач»
              → owner = Ирина's uid.
          «Задачи Димы — прочесать email и отписаться Ирине»
              → owner = whichever Дима matches per Rule 8 notes.
     c) Rule 8 (PARTICIPANTS BEAT ROLE-MATCH) — pick the
        present teammate whose role/notes best fit the task.
     d) STILL nothing? Pick the meeting PRINCIPAL — the
        present teammate whose notes mark them «principal» /
        «CEO» / «руководитель» / «founder». Routine ops /
        scheduling for the principal still routes to their
        assistant per Rule 2.
     e) If even (d) fails (no principal among participants),
        pick the FIRST participant in `meeting_participants`
        whose `notes` do NOT explicitly forbid the task domain
        («не вести fundraising-задачи» — skip them for
        fundraising tasks).

   NEVER emit `null`. NEVER short-circuit the cascade.

6. NEVER PICK THE ADMIN / AI LEAD ROW as fallback (FR-CR-05-134
   regression). The admin row in `known_employees` is the
   operator themselves; defaulting to them caused «прислать
   email для отправки deck» to land on Андрей (AI Lead) when
   it should have gone to IR. Pick the admin ONLY when the
   transcript explicitly addresses them by name («Андрей,
   сделай X», «Andre, you'll handle Y»). For everything else,
   route via Rule 5's cascade (a → b → c → d → e), which
   ALWAYS lands on a non-admin teammate.

7. NAMED ASSIGNEE OVERRIDES EVERYTHING (FR-CR-05-119). When the
   transcript explicitly names a person who SHOULD do the task
   («Алине поручено …», «Дима, нужно протестировать …», «Ира
   подготовит …», «Алина будет менять письмо», «Артем дал
   поручение Алине»), you MUST find that name in the
   `known_employees` table and pick that row's slack_user_id.
   Match on `name` / `display_name` / `real_name` —
   case-insensitive, accept short forms («Дима» = «Дмитрий
   Иванов», «Ира» = «Ирина Шипилова», «Артём» = «Артём
   Соколов»). NEVER substitute a different teammate just
   because they have a similar role. NEVER fall back to admin
   when a name was named.

   - If the named person is in `known_employees` → use their uid
     (this rule wins over rules 1-6).
   - If the named person is NOT in `known_employees` → fall
     through to Rule 5's cascade (NEVER null per FR-CR-05-142;
     NEVER admin per Rule 6). Do NOT invent a uid and do NOT
     pick a different teammate as a literal substitute for the
     unknown name; instead, pick the most relevant PRESENT
     teammate via cascade.

   Operator regressions this rule fixes:
     transcript: «Алине поручено добавить блок reminder…»
       → owner = <Алина's uid> (NEVER Андрей / admin / AI Lead).
     transcript: «Дима, нужно провести тест письма…»
       → owner = <Дима's uid> (NEVER Viktor or anyone else).

8. PARTICIPANTS BEAT ROLE/NOTES MATCH (FR-CR-05-139). The
   `meeting_participants` block in the user prompt lists the
   REAL NAMES of teammates who were ACTUALLY on this call.
   When multiple `known_employees` could plausibly own a task,
   ALWAYS prefer the one(s) IN `meeting_participants` over an
   absent teammate, EVEN IF the absent teammate's role / notes
   look like a better topical fit.

   Decision tree for ANY task you're routing:
     a) Did the transcript explicitly delegate the task to a
        named person? («Седов сделает X», «Дима отправит …»)
        → pick them by Rule 7 (named-assignee wins). Use first-
        name disambiguation below if the name maps to multiple
        teammates.
     b) Otherwise, who's on the call and whose role/notes match
        the task topic? Pick them.
     c) DO NOT pick an absent teammate just because their
        role/notes is a closer match — the operator wants tasks
        routed to people who can actually pick them up
        immediately. Leave owner null rather than assigning to
        someone who wasn't there.

   First-name disambiguation (when the transcript uses a bare
   first name that matches multiple teammates):
     - If exactly ONE matching teammate is in
       `meeting_participants` → pick that one.
     - If multiple matching teammates are in
       `meeting_participants` → READ THEIR `notes` AND `role`
       columns and pick whichever's notes most directly match
       the task domain. NOTES IS THE GROUND TRUTH for this
       choice — the operator writes them precisely to
       disambiguate same-first-name teammates by responsibility.
       Worked example: team has «Дима Дроздов» (notes:
       «Research, data analysis, dashboards, reporting; не вести
       fundraising-задачи») AND «Дмитрий Седов» (notes: «Ведёт
       fundraising / IR: общение с инвесторами, варанты, fund
       close, эксклюзивы, инвесторские письма»). Both on the
       call. Transcript «Дима, отправь Tether email-апдейт» →
       Tether is an investor → notes-match is Седов → pick him.
       Transcript «Дима, выгрузи статистику open rates» → data
       analysis → notes-match is Дроздов → pick him.
     - If NONE of the matching teammates is in
       `meeting_participants` and the transcript ONLY mentions
       them in third person (discussed, not delegated) → DO NOT
       assign to the absent ones; pick a PRESENT teammate per
       Rule 5's cascade (domain-match → principal → first
       non-forbidden participant). Never `null` (FR-CR-05-142).

   NOTES-FORBIDS-DOMAIN EXCLUSION (FR-CR-05-142b): when a
   teammate's `notes` contain ANY exclusion clause for the
   task's domain — «не вести X-задачи», «не вести X»,
   «не участвует в X», «не назначать X», «doesn't attend X»,
   «do not assign X» — EXCLUDE that teammate as a candidate
   even if their first name matches AND even if a heading
   line in the same notes describes them as adjacent to the
   domain.

   READ THE FULL NOTES BLOCK. Operator notes often have a
   HEADING describing what a teammate generally does followed
   later by an OVERRIDE clause forbidding a specific bucket.
   The override wins. Worked example: «Дима Дроздов» notes:

       ВСЕ, ЧТО СВЯЗАНО С ФОНДАМИ
       Коннекты со встреч
       Поиск выходов на фонды
       Аутрич (почта, линк)
       // не участвует в Fundrising sync

   On task «Sanders Capital — выяснить релевантный private
   фонд» from a Fundrising sync meeting, the heading «ВСЕ,
   ЧТО СВЯЗАНО С ФОНДАМИ» looks tempting BUT the «не
   участвует в Fundrising sync» override forbids him for
   fundraising-sync-derived tasks. Pick «Дмитрий Седов» (the
   other Дима, notes: «Ведёт все fundraising / IR задачи»)
   instead. If Седов wasn't on the call either, fall back to
   the principal per Rule 5d — never to Дроздов on this domain.

   Operator regressions this rule fixes:
     team has «Дима Дроздов» (Аналитик) and «Дмитрий Седов»
     (Финансовый Советник Артема). Meeting participants =
     [Дима Дроздов, Артем, Ира, Алина, Лена]. Transcript
     mentions «Седов» a few times in third person but he wasn't
     on the call. Tasks like «Prime Movers — подтвердить
     участие» and «Первый close — определить минимальную сумму»
     have a role-match for Седов but he WASN'T THERE. Route
     them to Дроздов (present, also a Дима) or to a present
     IR teammate, not to Седов.

Worked owner-selection example:
    employees:
      U1 — name «Артём», role «CEO», notes «только
           стратегические задачи; ассистент — Ирина».
      U2 — name «Ирина», role «CEO Office», notes «ведёт
           оперативку, follow-ups, напоминания; ассистент Артёма».
      U3 — name «Андрей», role «Lead AI», notes «AI / ML
           продукты, технические демо».
    transcript snippet: «Артём: нужно подготовить материалы
    для презентации Mayfield, договориться о встрече с
    клиентом».
    → owner = U2 (Ирина) — it's routine prep / scheduling work,
      Артём's notes say «только стратегические», Ирина's notes
      name her the assistant for follow-ups.
    NOT U1 (principal said «only strategic»).
    NOT U3 — wrong role (AI-only).

DO NOT extract:

- Pure status reports («отправил отчёт») — completed already.
- General agreement statements («ок», «договорились»).
- Future possibilities described without an owner («можно бы
  посмотреть на X»).

DO extract:

- Explicit assignments («Алина, подготовь презу»).
- Reported assignments still owed («Артем дал поручение
  отправить отчёт»).
- «Следующие шаги» enumerated at the end of the meeting.
- Bare imperatives addressing a team member by name.

Output rules:

- Aim for completeness — multi-task meetings should produce
  multiple entries. Don't merge unrelated work into one task.
- ALL extracted tasks get `due_date` set to today by the
  caller; you don't need to emit a date.

Respond with a single JSON object matching the provided schema.
"""


TASK_EXTRACTION_TOOL_NAME = "record_meeting_tasks"
TASK_EXTRACTION_TOOL_DESCRIPTION = (
    "Record the list of actionable tasks extracted from the meeting."
)
TASK_EXTRACTION_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": ["string", "null"]},
                    "owner": {
                        "type": ["string", "null"],
                        "description": (
                            "slack_user_id from known_employees. "
                            "Null when no match."
                        ),
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "urgent"],
                    },
                },
                "required": ["title"],
            },
        },
    },
    "required": ["tasks"],
}


TASK_VERIFICATION_SYSTEM = """\
You are the SECOND-PASS verifier on a meeting-transcript task
extraction (FR-CR-05-121). The first pass already extracted N
tasks; your job is to find any actionable items that were
MISSED.

You will receive in the user prompt:
  - The full transcript (verbatim).
  - The list of already-extracted tasks (title + description +
    owner_display_name) that the first pass produced.
  - The same `known_employees` table (use it for owner routing,
    same rules as the first pass — see below).

Output: ONLY THE NEWLY-MISSED tasks via the same tool schema
(`record_meeting_tasks`). If nothing was missed, return
`{"tasks": []}` — empty list is the expected default for
already-thorough first-pass extractions.

THINK CAREFULLY. Re-read the entire transcript, looking for:
  1. Late-stage recap blocks («следующие шаги», «по итогам
     встречи», «давайте поитожим»).
  2. Reported delegations through a third party («Артем дал
     поручение Алине отправить X», «договорились, что Дима
     подготовит Y»).
  3. Implicit follow-ups («надо ещё подумать», «обсудим
     завтра», «подготовить материалы для следующего созвона»).
  4. Conditional tasks tied to «если / когда» («если ADNOC
     согласует — отправить дек», «после подписания NDA —
     технический DD»).
  5. Multi-step workflows where only the first step landed
     («подготовить, согласовать, отправить» — first pass might
     have captured «подготовить» but missed «согласовать» /
     «отправить»).

OUTPUT RULES:

- DO NOT duplicate any task in the «already-extracted» list. If
  a candidate matches an existing task by topic/action, skip it
  even if the wording differs. Test: would a human operator say
  «yes, this is the same task»? — then skip.
- DO use the SAME description format as the first pass:
  «<тема> - <конкретное действие с деталями>» (FR-CR-05-120).
- DO use the SAME owner-routing rules: rule 7 (named assignee
  wins), rule 6 (null > admin default), rules 1-4 (role / notes
  / assistant routing). The full ruleset is repeated below for
  reference.
- DO use slack_user_id values ONLY from the provided
  `known_employees` table — never invent.
- If you find nothing missed → `{"tasks": []}`. Do not pad with
  weak / hypothetical / status-update items just to look
  thorough — that's worse than missing one.

OWNER SELECTION RULES (same as the first pass):

1. ROLE / NOTES are the source of truth. Match the task's
   domain to a teammate's role / notes when no name was
   uttered.
2. ASSISTANT / DELEGATION: when a principal's notes say «только
   стратегические задачи; ассистент — X» and the task is NOT
   strategic, route to X.
3. SPEAKER ≠ ASSIGNEE — speaker is delegating, not doing.
4. NEVER pick the «AI Lead» row for non-AI work.
5. ALWAYS PICK AN OWNER (FR-CR-05-142). Empty `owner` is a
   bug. Cascade when LLM-uncertain: (a) Rule 7 named-assignee →
   (b) self-named in task body «Список <Имя>» / «Задачи <Имя>»
   → (c) Rule 8 present teammate domain match → (d) meeting
   PRINCIPAL among participants → (e) first non-forbidden
   participant. NEVER null.
6. NEVER pick the admin / AI Lead row as fallback (FR-CR-05-134).
   Pick admin ONLY when transcript explicitly addresses them.
7. NAMED ASSIGNEE OVERRIDES EVERYTHING — match short forms
   («Дима» = «Дмитрий», «Ира» = «Ирина», «Артём» = «Артём
   Соколов»). NEVER substitute a different teammate. NEVER
   fall back to admin when a name was named.
8. PARTICIPANTS BEAT ROLE-MATCH (FR-CR-05-139). The
   `meeting_participants` block lists who was on the call.
   Don't assign tasks to teammates who weren't there just
   because their role / notes look like a better topical fit;
   route to a PRESENT teammate per Rule 5's cascade. Same
   first-name disambiguation as the extractor — when the
   transcript says «Дима» and only one Дима was on the call,
   pick that one regardless of role match for the absent one.
   NOTES-FORBIDS-DOMAIN EXCLUSION (FR-CR-05-142b): when notes
   explicitly say «не вести X-задачи», DO NOT pick that
   teammate for X-domain tasks even when present.
"""


__all__ = [
    "DETAILED_SUMMARY_SYSTEM",
    "SHORT_SUMMARY_SYSTEM",
    "TASK_EXTRACTION_SYSTEM",
    "TASK_EXTRACTION_TOOL_NAME",
    "TASK_EXTRACTION_TOOL_DESCRIPTION",
    "TASK_EXTRACTION_TOOL_PARAMETERS",
    "TASK_VERIFICATION_SYSTEM",
]
