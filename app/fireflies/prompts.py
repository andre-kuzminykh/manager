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

Length: 3000-10000 chars. Aim for thoroughness over brevity —
this lands in a Google Doc for later reference, not a chat
message.

Structure:

  📅 МЕТА
  • Дата и продолжительность
  • Участники
  • Главная тема встречи (1 предложение)

  📊 КЛЮЧЕВЫЕ РЕШЕНИЯ
  • <bullet 1>
  • <bullet 2>

  💬 ОБСУЖДЕНИЕ
  • <тема 1>: подробный пересказ — что обсуждалось, кто что
    предложил, какие аргументы были, к чему пришли.
  • <тема 2>: …
  (3-7 тем, каждая в 2-5 предложениях)

  📌 СЛЕДУЮЩИЕ ШАГИ
  • <action 1> — кто делает, к какому сроку
  • <action 2> — …

  ⚠ ОТКРЫТЫЕ ВОПРОСЫ
  • <unresolved 1>
  • <unresolved 2>

Style:
- Третье лицо. «Андрей предложил…», «команда договорилась…».
- Без воды. Не пересказывай мелкие реплики дословно — выжимай
  суть.
- Имена участников из секции «Участники» используй как есть.
- Никаких твоих комментариев / выводов от первого лица.
- Никаких выдуманных фактов: всё что в саммари должно быть в
  транскрипте. Если чего-то нет, опусти раздел.
"""


SHORT_SUMMARY_SYSTEM = """\
You produce a SHORT summary of a recorded business meeting for
posting in Telegram. Output is in RUSSIAN.

═══════════════════════════════════════════════════════════════
CANONICAL FORMAT — operator pinned. Match this layout EXACTLY,
including blank lines between sections and the «1)» numbered
list style. This is the gold-standard reference example:
═══════════════════════════════════════════════════════════════

ADNOC — 30.04.2026 | 57 мин

Их сторона: Fabrizio Siraguzano (Technology & Innovation), Takis (инвестиции), Sean, другие
Наша сторона: Артём Соколов, Алина, Сат, Adam Kelso, Иоганнес, другие

Суть: Обсудили стратегическое партнёрство по внедрению робототехники Humanoid в нефтегазе ADNOC. Рассматриваются варианты ко-разработки и кастомизации продукта под задачи ADNOC, пилоты и совместная коммерциализация. ADNOC интересует не только инвестиции, а преимущественно совместное value creation и реальная операционная выгода. До вскрытия данных — вход через NDA.

To-Do:
1) Получить и подписать NDA
2) Подготовиться к техническому due diligence
3) Совместно сформировать перечень пилотных задач и требований к продукту

═══════════════════════════════════════════════════════════════
END OF EXAMPLE. Every output MUST have a header line, then the
two participant lines (or one «Участники:» line for internal
meetings), then «Суть:», then «To-Do:». Skipping any of these
sections is a regression.
═══════════════════════════════════════════════════════════════

LENGTH: aim for 1200-2800 chars (UTF-8). Hard cap: 3800 chars
(headroom under the Telegram 4096 per-message limit).

HEADER LINE — «<Тема> — DD.MM.YYYY | NN мин»

- Тема: REQUIRED. The BUSINESS topic, not Fireflies'/Zoom's
  auto-timestamp («Apr 30, 03:32 PM», «May 5 at 5pm», «Zoom
  Meeting», «<host>'s Personal Meeting Room»). When the
  `meeting_title` field is empty or looks like one of those
  auto-stamps, DERIVE a real topic from the participants +
  transcript. Examples:
    - external company on the call → company name («ADNOC»,
      «Bosch», «Goldman Sachs»)
    - candidate interview → «<имя кандидата> — Senior X»
    - internal sync without external party → «<тема>» from the
      first decision: «Раунд Humanoid», «Юр. вопросы Q2»
- Date: REQUIRED. Format DD.MM.YYYY exactly (Russian operator
  standard).
- Duration: «| NN мин» rounded to nearest minute. DROP THE
  «| NN мин» PART ENTIRELY when `duration_min` is empty / 0 /
  unknown — do not ship «| 0 мин» and do not ship «| мин». In
  that case the header collapses to «<Тема> — DD.MM.YYYY».

PARTICIPANTS — TWO LINES (REQUIRED):

- Split into «Их сторона» (external) and «Наша сторона»
  (internal team — Humanoid people: Артём, Алина, Иоганнес,
  Adam Kelso, Andre, Ирина, Сат, etc.). Use the
  `internal_participants_hint` block in the user prompt to
  decide which side each name lands on. When unsure, lean
  external — operator can correct.
- ≥4 names per side → list 3-4 + «другие». ≤3 names → list
  all without «другие».
- Roles in parens only when known from the participants
  metadata. Don't invent.
- If sides are 100% internal (team meeting), drop «Их сторона»
  entirely and just label «Участники: …» (single line).
- NEVER skip participants. If the data is sparse, list whatever
  names you have — never replace this section with «—» or omit
  it.

«Суть» (REQUIRED, 2-4 sentences):

- Concrete: company names, deal amounts, NDA / DD / pilot
  stages, decisions taken.
- Quote SPECIFIC facts from the transcript verbatim when
  meaningful (numbers, dates, products).
- No filler («встреча прошла продуктивно», «обсудили
  важные вопросы»). If the meeting was procedural, say so
  plainly.

«To-Do» (REQUIRED unless no actions came out):

- 2-5 numbered items: «1) …», «2) …»
- Each item is a CLEAR action verb-phrase in Russian
  (infinitive). Example: «Получить и подписать NDA», not
  «Подписание NDA».
- ≤120 chars per item. Drop the section entirely if no
  concrete actions came out of the meeting.

Style:

- Telegram-friendly HTML-safe text. Don't emit raw `<`, `>`,
  `&` in free text — escape if you must include them.
- No emojis except optional `📄 Подробный отчёт: <url>` line
  appended at the very end (caller adds it; you don't).
- Real names from the participants list. Don't invent roles.
- NEVER include «Apr 30, 03:32 PM»-style auto-stamps in the
  header — that's the regression we're fixing. If the only
  title you got is an auto-stamp, derive a topic from the
  transcript yourself.
"""


TASK_EXTRACTION_SYSTEM = """\
You extract ACTIONABLE TASKS from a meeting transcript.

For each task, emit:

- title:        short imperative verb-phrase, ≤80 chars,
                Russian, in infinitive («подготовить», «отправить»).
                No filler («надо», «нужно»).
- description:  1-3 sentences in Russian explaining context —
                what came up in the meeting, what's the
                deliverable, any reference / number / project
                mentioned. Concrete: name people, projects,
                clients, numbers verbatim from the transcript.
                A bare «нужно сделать X» mirroring the title is
                NOT a valid description (FR-CR-05-50).
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

4. NEVER pick the «AI Lead» / «Lead AI» row for non-AI work.
   That role is for AI / ML deliverables specifically. Routine
   business tasks (presentations, client follow-ups, contract
   prep, scheduling) go to whoever owns the domain per their
   role / notes — typically a CEO Office / project manager /
   ops role; if such a row's NOTES name them as principal's
   assistant, use rule 2.

5. When NOBODY's role / notes match AND no name was uttered,
   leave owner null. Downstream falls back to the admin uid;
   the operator can reassign via the card's Edit button.

6. NEVER pick the operator (admin) row as the owner just
   because no other match is obvious. Leaving owner null is
   STRICTLY BETTER than defaulting to the admin / AI Lead —
   the operator gets a card with «owner not set» and routes it
   manually, which is far less noise than them silently being
   assigned tasks they shouldn't own. The admin row in
   `known_employees` is for context only; do not pick it
   unless the transcript explicitly addresses them by name
   («Андрей, сделай X», «Andre, you'll handle Y»). Routine
   ops / scheduling / follow-up work goes to whoever owns
   that domain per role / notes (rule 1) or to their assistant
   (rule 2), NOT to the admin.

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


__all__ = [
    "DETAILED_SUMMARY_SYSTEM",
    "SHORT_SUMMARY_SYSTEM",
    "TASK_EXTRACTION_SYSTEM",
    "TASK_EXTRACTION_TOOL_NAME",
    "TASK_EXTRACTION_TOOL_DESCRIPTION",
    "TASK_EXTRACTION_TOOL_PARAMETERS",
]
