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

LENGTH: aim for 1500-3500 chars (UTF-8). Hard cap: 3800 chars
(left ~10% headroom under the 4096 Telegram per-message
limit). Operator wants the short DM to be informative on its
own — don't truncate to 800-char teasers when the meeting
genuinely had several decisions and follow-ups.

Structure:

  🎙 <название встречи>
  📅 <дата> · <продолжительность>

  👥 Участники:
  • <имя> (<роль или email если есть>)
  • <имя> …
  (одна строка на участника, как они переданы в user_prompt
  в секции `participants`. Если ролей нет — только имя.)

  📊 Ключевые решения:
  • <решение 1>
  • <решение 2>
  • <решение 3>
  (3-7 пунктов, каждый ≤200 chars; если решений почти не было
  — назови раздел «Что обсудили» и перечисли темы)

  💬 Главные обсуждения:
  • <тема 1> — 1-2 предложения, что обсудили, чем кончилось.
  • <тема 2> — …
  (опционально, добавь если есть что выжать; 2-4 пункта)

  📌 Следующие шаги:
  • <action 1> — кто
  • <action 2> — кто

  📄 Подробный отчёт: <google_doc_url>

The user prompt will give you `participants` and
`google_doc_url` to splice in. Drop the «Подробный отчёт» line
if the URL placeholder is empty. Keep the «Участники» section
even if the list is short — operator explicitly wanted to see
who was on the call straight from the DM.

Style:
- Telegram-friendly HTML-safe text. Don't emit raw `<`, `>`,
  `&` in free text — escape if you must include them.
- Bullets ≤200 chars; for «Главные обсуждения» 2 sentences max.
- Don't pad to fill the limit, but don't undershoot either —
  3-4 bullet sections is the target.
- Real names from the participants list. Don't invent roles
  that weren't given.
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
                mentioned. Same style as the regular task
                extractor (FR-CR-05-22 rules apply: no
                placeholder pronouns, no first-person plural,
                concrete names from the transcript).
- owner:        slack_user_id of the person responsible. Pick
                from the `known_employees` table provided in the
                user prompt. Match on real_name / display_name
                / role / notes — same DISAMBIGUATION rules as
                the regular owner extractor (FR-CR-05-31).
                Leave null when no row matches; downstream code
                will fall back to the admin.
- priority:     "low" | "medium" | "high" | "urgent". Use
                «urgent» only when someone said «срочно» / «ASAP»
                / «горит». Default «medium».

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
