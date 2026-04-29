"""Stage 2a of the intent pipeline — title / description / priority.

Runs concurrently with owner extraction (Stage 2b). The date is
resolved deterministically in Python (Stage 2c) and is intentionally
NOT part of this prompt.
"""
from __future__ import annotations

from typing import Any

TITLE_SYSTEM_PROMPT = """\
You extract a STRUCTURED TASK DRAFT from a Slack message. The message
has already been confirmed to be a task — your job is the structured
summary, nothing else.

Produce three fields:
- title:        short imperative summary of the work to do. Keep it
                tight (<= 80 chars; ideally 4-7 words). Strip
                wrappers like "надо", "please", "мне нужно". Strip
                any date / deadline phrasing — dates belong in a
                separate field. Use the imperative form
                ("подготовить питчдек", "prepare pitch-deck").

                NEVER quote large fragments — no email bodies,
                screenshot transcripts ("На изображении письмо…"),
                templates ("Hi [Name], reaching out as a fellow…"),
                URLs, or multi-paragraph dumps. If the source is
                that kind of artefact, the title MUST summarise it
                in <= 80 chars («отправить blurb для Abundance»,
                «обработать скриншот письма»). When the message is
                purely a quoted artefact with no clear action verb,
                it's NOT a task — return `is_task=false` upstream
                instead of stuffing the quote into the title here.
                Same for messages that are themselves questions
                without an imperative («это была задача?») —
                upstream classifier should mark them no_action;
                if you somehow get one anyway, give it the title
                «уточнить статус задачи».
- description:  any supplementary detail present in the source
                message — references, numbers, sub-items, rationale.
                Also use it for the leading project / context tag
                of a note-style input («Olayan — …») and for the
                assigner of a reported assignment («по поручению
                Артема»). null when the title already captures
                everything. Do NOT copy the date phrase here either.
                Don't invent a description from a stray name out of
                nowhere — only set it when the source message
                actually carries the tag / assigner.
- priority:     one of "low" | "medium" | "high" | "urgent".
                Default "medium". Use "urgent" only when the author
                says so ("срочно", "ASAP", "blocker", "сегодня же"),
                "high" for "важно"/"important".

Examples (every date-like tail is removed from the title):
  "мне нужно купить машину ровно через три недели"
      → title: "купить машину"
  "надо подготовить питчдек к 1 мая"
      → title: "подготовить питчдек"
  "Иван, сделай отчёт до пятницы"
      → title: "сделать отчёт"
  "please prepare the slides by Friday"
      → title: "prepare the slides"
  "через две недели нужно запустить лендинг"
      → title: "запустить лендинг"
  "отправь письмо завтра утром"
      → title: "отправить письмо"

NOTE-STYLE INPUTS — drop the leading context tag, keep the action:
  "Olayan — напомнить Татьяне про контакт"
      → title: "напомнить Татьяне про контакт"
        description: "Olayan"  (the project / meeting tag)
  "Q3 review — подготовить slides"
      → title: "подготовить slides"
        description: "Q3 review"
  "Acme: send NDA"
      → title: "send NDA"
        description: "Acme"

REPORTED ASSIGNMENTS — drop the «X told me to» wrapper, keep the
actual work in the title; note the assigner in the description so
the trace isn't lost:
  "Артем дал поручение — отправить отчёт"
      → title: "отправить отчёт"
        description: "по поручению Артема"
  "Petya asked me to prepare Y"
      → title: "prepare Y"
        description: "asked by Petya"

A name is only a stripped *assignee* when it stands at the start
in vocative / @-mention form, or appears in dative ("Ивану", "to
Ivan"). A name in **accusative case** ("ивана", "машу" — i.e. the
*object* of the verb) MUST stay in the title — that person is
the subject of the work, not the doer. Examples:
  "подготовить ивана к среде"
      → title: "подготовить ивана"   (Иван is the object — keep)
  "Иван, подготовь презу"
      → title: "подготовить презу"   (Иван is the assignee — drop)
  "@petya сделай отчёт"
      → title: "сделать отчёт"       (explicit @ mention — drop)

Do NOT try to extract the assignee or the due date — those are
handled in separate passes. It is fine to leave hints about them in
the description if the message mentions them.

Copy only text that appears in the source_message (or its immediate
context). Do not invent details.

Respond with a single JSON object matching the provided schema.
"""

TITLE_TOOL_NAME = "record_task_draft"
TITLE_TOOL_DESCRIPTION = "Record the title / description / priority for a task."
TITLE_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": ["string", "null"]},
        "priority": {
            "type": "string",
            "enum": ["low", "medium", "high", "urgent"],
        },
    },
    "required": ["title"],
}


def build_title_user_prompt(
    *,
    source_text: str,
    context_messages: list[dict],
) -> str:
    lines: list[str] = []
    if context_messages:
        lines.append("context (oldest first):")
        for m in context_messages:
            user = m.get("user") or "unknown"
            text = (m.get("text") or "").replace("\n", " ").strip()
            ts = m.get("ts") or ""
            lines.append(f"- [{ts}] {user}: {text}")
        lines.append("")
    lines.append("source_message:")
    lines.append(source_text)
    return "\n".join(lines)


__all__ = [
    "TITLE_SYSTEM_PROMPT",
    "TITLE_TOOL_NAME",
    "TITLE_TOOL_DESCRIPTION",
    "TITLE_TOOL_PARAMETERS",
    "build_title_user_prompt",
]
