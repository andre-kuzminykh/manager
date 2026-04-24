"""Stage 2c of the intent pipeline — due-date extraction.

Dedicated LLM call (gpt-4o by default — stronger than mini on relative
dates). Output is a plain ISO date or null. A Python validator
(date_resolver.resolve_due_date) runs in parallel as a safety net: if
the LLM returns null for text that clearly contains a date phrase,
the validator's answer is used; if the LLM returns something invalid
(non-ISO, back-dated without "сегодня"/"today"), it is replaced.
"""
from __future__ import annotations

from typing import Any

DATE_SYSTEM_PROMPT = """\
You extract the DUE DATE from a Slack task message.

Output a single JSON object with:
- due_date: ISO string YYYY-MM-DD, or null if no date is stated.
- reasoning: one sentence quoting the wording you resolved.

Rules:
1. Resolve every date against the provided current_date. Never emit a
   date in the past unless the user literally wrote "сегодня"/"today".
2. Weekdays always mean the NEXT upcoming occurrence strictly after
   current_date. If today is Friday and the user says "к пятнице" or
   "by Friday", the answer is next Friday, not today.
3. Relative phrases:
     завтра / tomorrow                  → current_date + 1
     послезавтра / day after tomorrow   → current_date + 2
     через N дней / in N days           → current_date + N
     через неделю / in a week           → +7
     через две недели / in two weeks    → +14
     через месяц / in a month           → +30 days (approx, same day next month)
     к концу недели / end of week       → upcoming Friday
     на этой неделе / this week         → upcoming Friday
     на следующей неделе / next week    → upcoming Monday
     к концу месяца / end of month      → last day of current month
     к концу года                       → 31 December of current year
4. Specific dates (Russian + English):
     "1 мая" / "к 1 мая"                → YYYY-05-01 (next future)
     "25 декабря" / "by Dec 25"         → YYYY-12-25
     "май" / "в июне" / "by May"        → 1st of that next-future month
     "01.05.2026" / "1/5" / "15.06.26"  → literal numeric (day first)
     "2026-05-01"                       → literal ISO
5. Written numbers count: "через две недели" = 14 days, "in three
   days" = 3 days.
6. Vague phrases stay null: "когда-нибудь", "скоро", "в ближайшее
   время", "some day", "asap".
7. Do NOT invent a date when the message contains no date cue.

Respond with a single JSON object matching the provided schema.
"""


DATE_TOOL_NAME = "record_due_date"
DATE_TOOL_DESCRIPTION = "Record the extracted due date for the Slack task."
DATE_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "due_date": {
            "type": ["string", "null"],
            "description": "ISO YYYY-MM-DD or null.",
        },
        "reasoning": {
            "type": "string",
            "description": "One-sentence justification quoting the date phrase.",
        },
    },
    "required": ["reasoning"],
}


def build_date_user_prompt(
    *,
    source_text: str,
    current_date: str,
    current_weekday: str,
) -> str:
    return (
        f"current_date: {current_date} ({current_weekday})\n"
        "\n"
        "source_message:\n"
        f"{source_text}"
    )


__all__ = [
    "DATE_SYSTEM_PROMPT",
    "DATE_TOOL_NAME",
    "DATE_TOOL_DESCRIPTION",
    "DATE_TOOL_PARAMETERS",
    "build_date_user_prompt",
]
