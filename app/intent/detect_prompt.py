"""Stage 1 of the intent pipeline — "is this message a task?".

A tiny yes/no classifier. It runs BEFORE any structured extraction so
small LLMs (gpt-4o-mini) don't have to juggle intent + title + owner +
date in a single prompt. If detection says no, the pipeline returns
no_action immediately and no further LLM calls are made.
"""
from __future__ import annotations

from typing import Any

DETECT_SYSTEM_PROMPT = """\
You are a binary classifier for Slack/Telegram messages. You answer
two questions in one shot:

1. Is the author asking someone to do a piece of work?
2. If yes — does the message describe ONE task or SEVERAL?

Return ``is_task=true`` for ALL of the following shapes:

- *Direct imperative / delegation:*
  "надо ...", "нужно ...", "сделай ...", "подготовь ...",
  "собери ...", "отправь ...", "напиши ...",
  "please do X", "can you send Y?", "prepare Z by Friday",
  "assign to <Name>".

- *Bare infinitive describing work* (very common in note-style
  todo dumps):
  "напомнить Татьяне про контакт", "подготовить отчёт",
  "позвонить Васе сегодня", "отправить договор",
  "send the deck", "follow up with Petya".

- *Note-list entries shaped `<context> — <action>`* — the dash /
  hyphen / colon separates a project-or-meeting tag from the work:
  "Olayan — напомнить Татьяне про контакт",
  "Q3 review — подготовить slides",
  "Acme: send NDA".

- *Reported assignments still owed* — someone delegated to the
  author and the work isn't done yet:
  "Артем дал поручение — отправить X",
  "Petya asked me to prepare Y",
  "получил задачу подготовить Z от Маши".

Return ``is_task=false`` for:
- Chat, greetings, reactions, jokes.
- *Completed* status reports ("отправил", "готово", "сделал X").
  Note: an UNFINISHED report of someone else's outstanding ask
  («Артем сказал отправить, я пока не успел») still COUNTS AS A
  TASK — the action is owed.
- *Status-list reports* — multiple parties' progress strung
  together with dashes / commas. Each item describes WHAT IS or
  WHAT ISN'T, not what to DO. Examples:
  «DBS — нет, Jefferies — отправила линки на регистрацию,
   Stifel — не ответил»,
  «Q1 done, Q2 in progress, Q3 not started»,
  «Petya — done, Masha — not yet, Vlad — blocked».
  These are read-only updates; they do NOT create tasks even
  when individual list items contain unfinished work.
- *Parroted acknowledgements / one-line replies* without context
  — «хорошо! напишу ему», «ок, сделаю», «договорились», «понял,
  займусь», «yes, will do». By themselves these are
  status-promises, not tasks. They become tasks ONLY when the
  surrounding context makes the actual work unambiguous (a prior
  message saying «нужен ответ на письмо XYZ»). Use the
  ``context`` block to decide. Without context, treat as
  no_action — better than capturing a vague widget.
- *OCR / transcription noise* — a single non-word artefact like
  «файндхэзом», «бумаусы», «zzzx» on its own is not a task.
  Reject when the message is a single token that doesn't form a
  recognisable Russian / English word and has no surrounding
  imperative.
- *Bare questions without an imperative*. A question is no_action
  when no one has to *do* anything to answer it:
  «как дела?», «что думаешь?», «это была задача?»,
  «ок?», «is this a task?», «есть встреча по демо?».
  A polite-ask imperative («can you send the deck?», «отправите
  отчёт?») IS a task — distinguish by whether the answer
  requires WORK or just YES / NO.
- *Pure quoted artefacts* — when the message is essentially a
  template / blurb / email body / screenshot transcript without
  an imperative wrapper («Блерб для отправки Abundance: Hi
  [Name]…», «На изображении письмо с темой …»). The artefact
  ITSELF isn't a task. If the author explicitly says «отправь»,
  the imperative is the task and the artefact is the description.
- Pure information ("доска в Figma: <link>").

Scope: tasks only. Meetings and calendar events are OUT of scope
here — they go through a separate pipeline.

Confidence in [0, 1]:
  0.90+   explicit imperative ("надо подготовить отчёт до пятницы")
  0.70-0.89  likely task — bare infinitive, note-list entry, or
             reported assignment
  0.40-0.69  might be a task, tone unclear
  <0.40   probably chat

MULTI-TASK SPLITTING (FR-CR-05-05):
A single message can describe several tasks. Split when each chunk
has its own imperative verb + object pair, often joined by "и"/"и
ещё"/"+"/", "/"and"/"plus":

  "к завтра сделать презу и отчёт к пятнице"
      → 2 tasks: ["сделать презу", "отчёт к пятнице"]
  "позвонить Васе сегодня и подготовить договор"
      → 2 tasks: ["позвонить Васе сегодня",
                  "подготовить договор"]
  "send the deck and call the client tomorrow"
      → 2 tasks: ["send the deck",
                  "call the client tomorrow"]

DO NOT split when the second clause is a *sub-item* of the first
(no second verb / object pair):

  "сделать отчёт и презентацию по нему"
      → 1 task — the second clause clarifies the first.
  "подготовить презу с графиками и таблицами"
      → 1 task — "графики и таблицы" describe the deck.
  "send the report and a brief follow-up note"
      → 1 task — the brief is part of the report.

Output rules:
- Always set ``task_count`` to a positive integer.
- For ``task_count > 1`` also set ``task_chunks`` — one verbatim
  span per task, copied from the source_message in the order they
  appear. Each span must be a contiguous substring of the source.
- For ``task_count == 1`` ``task_chunks`` may be omitted (the whole
  message is the chunk).

Respond with a single JSON object matching the provided schema.
"""

DETECT_TOOL_NAME = "record_detection"
DETECT_TOOL_DESCRIPTION = "Record the task / not-task verdict for the Slack message."
DETECT_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_task": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "reasoning": {"type": "string"},
        "task_count": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "Number of distinct tasks in the message. 0 when "
                "is_task is false."
            ),
        },
        "task_chunks": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "One verbatim source-text span per task, in order. "
                "Required when task_count > 1."
            ),
        },
    },
    "required": ["is_task", "confidence"],
}


def build_detect_user_prompt(
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
    "DETECT_SYSTEM_PROMPT",
    "DETECT_TOOL_NAME",
    "DETECT_TOOL_DESCRIPTION",
    "DETECT_TOOL_PARAMETERS",
    "build_detect_user_prompt",
]
