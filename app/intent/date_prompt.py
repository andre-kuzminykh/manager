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
1. Resolve every date against the provided current_date. **TODAY
   ITSELF IS VALID** — when the user says «сегодня» or the date
   resolves to current_date, emit current_date. Never push it
   forward to the next year just because today's "April 30"
   already happened earlier in the day. Operator regression:
   source had «обсуждалось в CEO Office · 2026-04-30 10:02» as
   metadata, LLM read «30 April» as past and emitted
   `2027-04-30` (a year off). The «not in the past» rule below
   means STRICTLY before current_date, not today.
   ONLY back-date when the user literally wrote
   "сегодня"/"today" AND there's a date phrase. For a date
   phrase like «1 мая» when today is 2 мая — emit next year's
   May 1. For «30 апреля» when today IS 30 апреля — emit today.
2. Weekdays always mean the NEXT upcoming occurrence strictly after
   current_date. If today is Friday and the user says "к пятнице" or
   "by Friday", the answer is next Friday, not today.
3. Relative phrases — understand WRITTEN numbers too:
     завтра / tomorrow                  → current_date + 1
     послезавтра / day after tomorrow   → current_date + 2
     через день                         → +1
     через 2 дня / через два дня        → +2
     через 3 дня / через три дня        → +3
     через неделю / in a week           → +7
     через две недели / in two weeks    → +14
     через три недели / in three weeks  → +21
     через пять недель / in five weeks  → +35
     через пару дней / a couple of days → +2
     через месяц / in a month           → +30 (same day next month)
     через два месяца / in two months   → +60
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
5. Count ANY written number (один/одну/одна, два/две, три, четыре,
   пять, шесть, семь, восемь, девять, десять, пара=2; one, two,
   three, four, five, six, seven, eight, nine, ten, a/an=1). Filler
   words like "ровно", "примерно", "около" between "через" and the
   unit don't change the count.
6. Vague phrases stay null: "когда-нибудь", "скоро", "в ближайшее
   время", "some day", "asap".
7. Do NOT invent a date when the message contains no date cue.
8. NEVER treat list-item / enumeration NUMBERS as dates. The
   patterns to ignore:
     "1. ", "2. ", "3. " at the START of a line (numbered list)
     "1)", "2)", "3)", "1/", "2/" (bullet variants)
     "пункт 5", "item 5", "section 3"
     "5." in front of a name / project («5. Мистраль — …»)
   These are bullet labels, not dates. They look like dates («5»
   ⇒ 5th of next month?) but they're structural. ONLY treat
   numbers as dates when accompanied by a temporal anchor:
     "к 5", "до 5", "к пятому", "5 мая", "5 May", "May 5",
     "5/05", "05.05.2026" — a month / weekday / «к» / «до» /
     «through» word MUST be present. Otherwise → null.

   Worked counter-example (operator regression):
     source: "5. Мистраль - Arthur Mehcsh - отправьте письмо…"
     BAD output: due_date=2026-05-05 (treats «5.» as 5th day)
     GOOD output: due_date=null (it's a list item, not a date)
9. NO HALLUCINATING DATES NOT IN THE SOURCE. If the message
   does NOT literally contain «следующая неделя» / «next week»
   / «понедельник» / «Monday» / «к понедельнику» / «by Monday»,
   you MUST NOT emit the upcoming Monday. The same goes for any
   other weekday — only emit a weekday's ISO date when the
   message explicitly names that weekday or a phrase that
   directly maps to it (table in rule 3).

   When the message has MULTIPLE candidate dates (e.g. «May 5
   6-9pm, May 6 9-12, May 8 9-12, May 9 5-7pm — pick a slot»),
   the message is offering OPTIONS, not a deadline. Emit null
   unless one of the slots is clearly singled out as «выбрали
   X» / «зафиксировали Y» / «final: Z». Never average, never
   pick "the earliest", never pick "the next Monday after
   today" — emit null and let the human resolve.

   Worked counter-example (operator regression FR-CR-05-82):
     current_date 2026-04-30 (Thursday)
     source: «Обсудить возможность встречи. Fubon предложили
              5 мая 18-21, 6 мая 9-12 или 17-19, 8 мая 9-12,
              9 мая 17-19. Думаю, возьмём 6 мая 11:30 London»
     BAD output:  due_date=2026-05-04 (next Monday — NOT in
                  source anywhere — pure hallucination)
     BAD output:  due_date=2026-05-05 (earliest of the slots —
                  the user did not say «к 5 мая», these are
                  meeting time candidates, not a deadline)
     GOOD output: due_date=null OR 2026-05-06 (only if the
                  «возьмём 6 мая» phrase is unambiguously a
                  decision; if it's «думаю» / «возможно»
                  / «давайте подумаем» — null is correct)
10. THE DATE MUST BELONG TO THE TASK ACTION, not to a
    different entity mentioned alongside it.

    Operator regression FR-CR-05-87:
      source: «Отредактировать письмо для MGX — убрать
               минимальный чек, и упомянуть что раунд нужно
               закрыть до конца мая»
      BAD output:  due_date=2026-05-31 (the «до конца мая»
                   refers to the ROUND's close deadline — a
                   business fact mentioned inside the email
                   content the user is asking us to edit. It
                   is NOT a deadline for the task itself.
                   That detail belongs in the description,
                   not the due_date.)
      GOOD output: due_date=null (the source did not state
                   when the EDIT must be done; downstream
                   defaults will set today 18:00).

    The discriminator: ask «which verb does this date
    modify?» When source says «X к 5 мая» / «to do X by
    May 5» — date modifies X, the task's verb. When source
    says «X — упомянуть, что Y до 5 мая» / «edit the email
    to mention that the round closes by May 31» — date
    modifies Y (the round, not the task). In the second
    pattern emit due_date=null.

    Other patterns that fall under this rule (date is NOT
    the task's deadline):
      - «отчёт о встрече 5 мая» — the meeting was on May 5
        (a past calendar event being reported on); the
        task is to write the report
      - «напомни Юле про вчерашний разговор» — «вчера»
        anchors the conversation, not the reminder
      - «подготовить материалы под раунд который закрываем
        до конца мая» — round closes end of May; the task
        is the prep, not the round
      - «обсудить с командой результаты квартала»  — the
        quarter (Q1, Q2 …) is a context window, not a
        deadline. Emit null.
      - «статус на DD.MM» / «status as of DD.MM» / «as of
        Feb 26» (FR-CR-05-94 — operator regression). This
        is the date a status update was last reported, NOT
        a deadline. Pattern: «статус на 26/02 — ждём
        ответа» = «as of Feb 26 we're still waiting» —
        emit null. Operator regression: «Узнать статус
        контакта … статус на 26/02 — ждем» landed as
        `due_date=2027-02-23` (year hallucinated AND date
        is a status-as-of-marker, not a deadline).

    If unsure whether the date modifies the verb of the task
    or another entity in the same sentence, EMIT NULL. The
    downstream default (`due_date=today 18:00`) is the safe
    fallback when context dates exist but don't apply.
11. PROOF QUOTE OR NULL (FR-CR-05-89). Operator policy:
    «либо в описание добавляй пруф либо сегодня». For every
    non-null `due_date` you emit, the `reasoning` field MUST
    start with a verbatim quote (≥4 chars, with surrounding
    context word if needed) of the date phrase from the
    source. Examples:

      source: «отчёт к пятнице»
      due_date: <Friday ISO>
      reasoning: «"к пятнице" — ближайшая пятница»

      source: «Подготовить письмо для MGX … упомянуть, что
              раунд закрыть до конца мая»
      ⇒ no exact phrase ties «до конца мая» to the EDIT verb.
        due_date: null
        reasoning: «нет явного дедлайна на правку письма;
                    "до конца мая" относится к раунду»

    If you cannot quote source verbatim, you have not
    earned the right to emit a date — emit null.

Worked examples:
  current_date 2026-04-24 (Friday)
  "мне нужно купить машину ровно через три недели" → 2026-05-15
  "надо запустить лендинг через две недели"         → 2026-05-08
  "отчёт через пять дней"                            → 2026-04-29
  "ship in a couple of weeks"                        → 2026-05-08
  "отправь завтра утром"                             → 2026-04-25
  "5. Мистраль — отправь письмо"                     → null

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
