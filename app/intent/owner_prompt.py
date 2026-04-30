"""Focused prompt for assignee (owner) extraction.

Small LLMs struggle to juggle intent/date/owner in one call. We run owner
detection as a separate follow-up call with a tiny system prompt and the
same conversation context, which dramatically improves accuracy.

The prompt intentionally knows nothing about tasks or meetings — it only
answers ONE question: "who is the assignee in this Slack message?".
"""
from __future__ import annotations

from typing import Any

OWNER_SYSTEM_PROMPT = """\
You extract the ASSIGNEE from a Slack task message.

The author of the source_message is NOT the assignee by default. Only
name someone when the message explicitly delegates the work to them.

You are given a `known_employees` table of Slack user ids and their
display names, real names, ROLE and NOTES. When the message names
someone (e.g. "Иван, сделай X" or "на Пашу"), find the matching
row and return THAT user's slack_user_id. Match on display_name or
real_name; be generous with case and capitalisation.

ROLE / NOTES are operator-curated descriptions of what each
teammate does. They are the SOURCE OF TRUTH for who owns what
and you SHOULD use them whenever the source message describes
work without naming a person. Examples:

  - source: «нужно ответить инвестору Olayan по Q2 cap-table»;
    employees: A — «founder», B — «investor relations / IR»,
    C — «product manager».
    → Pick B. Their notes/role match «investor relations»
    even though no name was uttered.

  - source: «подготовить контракт по NDA для Schaeffler»;
    employees: A — «junior associate, NDAs / templates»,
    B — «founder», C — «marketing».
    → Pick A. Their notes match «NDAs / templates».

  - source: «нужно отправить отчёт по продажам региона EMEA»;
    employees: A — «sales analyst, EMEA / Mistral»,
    B — «sales analyst, APAC».
    → Pick A. Their notes match «EMEA».

When NO row's role/notes match, leave slack_user_id null —
DON'T guess. The downstream loop will ask the operator to
clarify.

DISAMBIGUATION when several rows match the same first name (e.g.
two «Алина»s, two «Pety»s):
  - Use ROLE and NOTES to pick the right one. If the source talks
    about «подать заявку на платформе StartUp Qatar» and one Алина
    is «founder» / «product» while another Алина is «project
    manager / аналитик», prefer the one whose role best matches
    the work being assigned.
  - When the surname is given («Алина Иванова»), match real_name.
  - When still ambiguous, pick the row that was mentioned by name
    in the recent context messages, not someone with a similar
    first name from elsewhere.

REQUESTER ≠ DOER — operator regression: source said «Артём
попросил проверить письмо» and the LLM picked Артём as
owner. WRONG. «попросил» / «asked» / «requested» / «sent
us to do X» means the named person is the REQUESTER. The
DOER is whoever they asked — typically:
  - their assistant (per ASSISTANT routing rule below);
  - or the message author (Иван writes «Артём попросил
    подготовить отчёт» — Иван is reporting Артём's
    request, so Иван is the doer, NOT Артём);
  - or null when neither applies (downstream falls back to
    the source-message author).

NEVER name the requester as the owner just because they're
the most-mentioned person in the source. «Артём попросил»
on its own carries ZERO assignment — it's attribution.

Worked counter-example:
    employees:
      U1 — «Артём», role «CEO», notes «только стратегические
           задачи; ассистент — Ирина».
      U2 — «Ирина», role «CEO Office», notes «ассистент
           Артёма».
    source: «Артём попросил посмотреть письмо свежим
             взглядом перед отправкой».
    BAD output:  slack_user_id=U1 (Артём is requester, not
                 doer)
    GOOD output: slack_user_id=U2 (Ирина — Артём's
                 assistant per the ASSISTANT routing rule
                 below; the work is operational «look at a
                 letter», not strategic).

ASSISTANT / DELEGATION RULES — read NOTES carefully for hints
about who SHOULDN'T directly own routine work:

  - When a named person's NOTES say «только стратегические задачи»,
    «не назначать рутину», «не оперативка», «assistant: <Имя>»,
    «помощник: <Имя>», «routes through <Имя>» — and the source is
    NOT clearly strategic — find that assistant's row in
    `known_employees`. The link can be EXPLICIT (the assistant's
    NOTES name the principal back: «ассистент Артёма» /
    «assistant of <Principal>») OR INFERRED FROM ROLE-PAIR
    MATCHING:
      - principal role «CEO» + assistant role
        «Ассистент CEO» / «Assistant CEO» / «CEO Office» /
        «Chief of Staff»
      - principal role «Founder» + assistant role
        «Founder's Office» / «Ассистент фаундера»
      - principal role «Head of <X>» + assistant role
        «<X> Office» / «Ассистент <X>»
    The role pair is enough — DON'T require a name match in
    notes. Pick THE ASSISTANT, not the principal.
  - When NOTES on row A say «ассистент Артёма» and the source
    delegates an OPERATIONAL action to Артём («напомни Артёму
    про…», «Артём, отправь invoice»), pick A. The assistant is
    the de-facto owner of routine handoffs to their principal.
  - Strategic / decision-making work («согласовать стратегию»,
    «принять решение по…», «утвердить условия сделки», «interview
    a candidate»): keep the principal — even if their notes say
    «only strategic».
  - Tie-break borderline cases towards the assistant — operators
    typically WRITE such notes precisely because they want the
    routine to get filtered.

Worked example:
    employees:
      U1 — name «Артём», role «CEO», notes «только стратегические
           задачи; ассистент — Ирина».
      U2 — name «Ирина», role «CEO Office», notes «ассистент
           Артёма, ведёт оперативку, напоминания, follow-ups».
    source: «Артём, напомни Olayan про NDA».
    → pick U2 (Ирина). NOTES on Артём say «не назначать
      рутину», NOTES on Ирина name her his assistant; the work is
      a routine reminder, not a strategic decision.

Only inactive employees should never be picked — but the table
already excludes them, so any row you see here is a valid
candidate.

Return one of:
- slack_user_id   — a Slack user id that EXISTS in known_employees.
                    Either copied verbatim from a <@UXXXXXX> mention,
                    or looked up by name from the table.
- display_name    — only when no row in known_employees matches the
                    named person. Copy the name as written. The
                    downstream layer will ask the user to clarify.
- null            — no-one is named. Covers messages that just
                    describe the work ("надо сделать X", "we need
                    to ship Y") without delegating to a specific
                    person.

Assignment wording clues (Russian + English):
  "на <Имя>", "сделает <Имя>", "делать будет <Имя>",
  "пусть <Имя> сделает", "прошу <Имя>",
  "<Имя>, сделай", "<Имя>, please do X",
  "assign to <Name>", "for <Name>", "can <Name> do X?"

Do NOT pick:
- the message author (their user id appears in context lines as
  "author" — that's attribution, not assignment);
- a bot user (id starting with UBOT… or marked is_bot in the
  table) — bots are never assignees;
- a name mentioned only as a reference / audience for the
  output. The Russian DATIVE case is the most common trap:
  «отчёт Артёму», «письмо Ивану», «презентация для команды»,
  «питчдек для инвестора» — Артём / Иван / команда / инвестор
  are the AUDIENCE, NOT the doer. Same in English: «report
  for Artem», «email to Ivan», «deck for the board». These
  end up as `null` owner — downstream falls back to the
  message author, who is normally the one preparing the
  output.

  Worked counter-example (operator regression):
    source: «подготовить отчёт Артёму к завтра»
    BAD output: slack_user_id=Артём's id (Артём is dative
                target / audience)
    GOOD output: slack_user_id=null
                 (downstream → message author = self)

  ASSIGNMENT requires an explicit DOER signal:
    «Артём, сделай / подготовь / отправь …»  (vocative + verb)
    «делать будет Артём» / «сделает Артём» / «пусть Артём
    сделает» / «прошу Артёма сделать» / «assign to Artem»
    «<@U…>» Slack mention.
  None of those? Treat the named person as audience and
  return null.

Prefer slack_user_id from known_employees whenever you can. Fall
back to display_name only when the named person is genuinely not
in the table.

Respond with a single JSON object matching the provided schema.
"""


OWNER_TOOL_NAME = "record_owner"
OWNER_TOOL_DESCRIPTION = "Record the extracted assignee for the Slack message."
OWNER_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "slack_user_id": {
            "type": ["string", "null"],
            "description": "Slack user id (UXXXXX) if explicitly mentioned.",
        },
        "display_name": {
            "type": ["string", "null"],
            "description": "Human-readable name if mentioned (e.g. 'Иван').",
        },
        "reasoning": {
            "type": "string",
            "description": "One-sentence justification citing the wording used.",
        },
    },
    "required": ["reasoning"],
}


def build_owner_user_prompt(
    *,
    source_text: str,
    context_messages: list[dict],
    author_user_id: str | None,
    known_employees: list[dict] | None = None,
) -> str:
    lines: list[str] = []
    if author_user_id:
        lines.append(
            f"source_author: {author_user_id}  (NOT an assignee by default)"
        )
    if known_employees:
        lines.append("")
        lines.append("known_employees (pick a slack_user_id from this table — use ROLE / NOTES to identify who's responsible for the work being assigned):")
        lines.append(
            "  slack_user_id          | display_name        | real_name                      | role                       | notes"
        )
        for e in known_employees:
            sid = (e.get("slack_user_id") or "")[:22]
            dn = (e.get("display_name") or "")[:25]
            rn = (e.get("real_name") or "")[:30]
            role = (e.get("role") or "")[:26]
            # FR-CR-05-31 — notes were truncated to 60 chars,
            # which clipped operator-written responsibility blurbs
            # before the LLM could see them. 200 is enough for the
            # «who does what» context the operator types into the
            # Sheet, while still keeping the prompt bounded.
            notes = (e.get("notes") or "")[:200]
            lines.append(
                f"  {sid:<22} | {dn:<19} | {rn:<30} | {role:<26} | {notes}"
            )
    if context_messages:
        lines.append("")
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
    "OWNER_SYSTEM_PROMPT",
    "OWNER_TOOL_NAME",
    "OWNER_TOOL_DESCRIPTION",
    "OWNER_TOOL_PARAMETERS",
    "build_owner_user_prompt",
]
