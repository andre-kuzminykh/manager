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
- *Completed* status reports — work that's already done, in any
  voice / tense:
    - active past: «отправил», «сделал», «закрыл», «позвонил»,
      «написал», «написала», «случайно отправила», «случайно
      сделал» (the «случайно» / «accidentally» modifier still
      reports past completion — FR-CR-05-94 regression: «Да,
      Юля случайно отправила» landed as a task; should be
      no_action). A leading «Да, » / «Yes, » CONFIRMS the
      preceding question and the past-tense verb that follows
      reports what happened — still completion recap.
    - already-prefix (FR-CR-05-93 — operator regression): any
      verb prefixed with «уже» / «already» reports completion,
      not new work — «уже написала», «уже отправила», «уже
      сделал», «уже подтвердил», «already sent», «already
      called». Even if the SAME message also says «и Y тоже»
      / «and Y too», it's still a status recap.
    - passive past: «отправлено», «отправлены», «сделано»,
      «подписан», «закрыт», «утверждён», «оплачен»
    - present-perfect English: «sent», «done», «closed»,
      «approved», «signed»
  Examples that are NOT tasks:
    «письма в Abundance отправлены» — пассивный отчёт, дело уже
    сделано;
    «договор подписан вчера» — done, no action owed;
    «отчёт готов, скинул в чат» — completion announcement.
  Operator regression (FR-CR-05-93) pinned as a worked
  failure-mode:
    source line 1: «Уже написала на почту ему тоже»
    source line 2: «ну ничего) и инвайт отправила»
    → is_task=false (BOTH lines are «уже X» recap; the
      «ну ничего)» interjection is chat noise, not an
      imperative; «и инвайт отправила» is more recap, not
      a separate action).
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
- *Opinion / qualifier statements without a clear imperative*
  (FR-CR-05-94 — operator regression). Sentences like:
    «По X я не против, но Y»  (qualified consent)
    «Они у Алины в задачах есть»  (status info — task is
        elsewhere)
    «Мне кажется, это надо обсудить»  (opinion)
    «I think we should look at X»  (opinion)
    «Они уже работают над этим»  (status info)
  describe the author's POSITION on something or report
  someone else's status — they are not delegating new work.
  Operator regression: «По Сингапуру и Гонконгу я не против,
  но у нас Алина — Chief of Investment Relations, я как
  Project Manager — координирую задачи, поэтому нужен апрув
  от нее и Артема на фонды в Гонконге и Сингапуре» landed as
  a 250-char title with no description. The «нужен апрув от
  нее и Артема» reads like a request for approval from
  someone else — that's not the AUTHOR's task, that's a
  qualifier. is_task=false.
- *Third-party future-intent reports* (FR-CR-05-95 — operator
  regression). Sentences like:
    «Они сами отправят ссылку»          (3rd party will do it)
    «Артем сам пришлёт»                  (3rd party will do it)
    «Ира потом перешлет»                 (3rd party will do it)
    «They will send the link themselves» (3rd party will do it)
  describe what someone ELSE plans to do — there's no work
  owed by anyone the author is delegating to. is_task=false.
- *Emotional / chat outbursts* (FR-CR-05-95). Sentences with
  no concrete deliverable, just emotional commentary:
    «Очень важный день. Надо помолиться или что ты делаешь
     в таких случаях»  (rhetorical chat noise)
    «Жду с нетерпением!»
    «Вот это да, неожиданно»
    «Wow, that's intense»
  even when they look question-shaped, are NOT tasks.
- *Pure quoted artefacts* — when the message is essentially a
  template / blurb / email body / screenshot transcript without
  an imperative wrapper («Блерб для отправки Abundance: Hi
  [Name]…», «На изображении письмо с темой …»). The artefact
  ITSELF isn't a task. If the author explicitly says «отправь»,
  the imperative is the task and the artefact is the description.
- *Transcription / chat dumps* — HARD RULE (FR-CR-05-89). When
  the source is a paragraph DESCRIBING what someone else did,
  said, or what's visible in an image, it is NOT a task. These
  are observation / commentary, no action owed. Reject lead-in
  patterns:
    «На изображении показано / На скрине …»  (image transcript)
    «На фото видно / На картинке …»          (image transcript)
    «Обсуждают / Обсуждается / Discussion of / In the chat …»  (chat dump)
    «Сообщение / Message from X: …»          (forwarded msg dump)
    «В переписке / В треде / В диалоге …»    (thread dump)
    «По переписке / По обсуждению …»         (recap; exception
        below — recap+«нужно сделать» IS a task)
    «Это что? / What is this? / Что это?»    (clarification
        question, the user is asking ABOUT the artefact)
  Worked failure-mode examples (operator regressions):
    source: «Это что? На изображении показано электронное
            письмо от Артема Соколова, отправленное Джоди и с
            копией Ирине …»
       → is_task=false (the user is asking what the
         screenshot is, not delegating work)
    source: «Обсуждают сообщения внутри группы CEO Office с
            Ириной. Ирэн сообщает, что Артём, скорее всего,
            не летит, и спрашивает источник …»
       → is_task=false (chat-content recap, no imperative)
  Exception: if the source contains BOTH a transcript AND an
  explicit action verb («Это письмо от Олаяна — ОТПРАВЬ ему
  ответ»), the action is the task and the transcript is its
  description. Confidence ≥0.75 only when the imperative is
  unambiguous; otherwise emit no_action.
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
