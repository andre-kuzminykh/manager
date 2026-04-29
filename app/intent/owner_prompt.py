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
- a name mentioned only as a reference, e.g. "питчдек для Ивана"
  (Ivan is the AUDIENCE, not the doer).

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
        lines.append("known_employees (pick a slack_user_id from this table):")
        lines.append(
            "  slack_user_id          | display_name        | real_name                      | role                       | notes"
        )
        for e in known_employees:
            sid = (e.get("slack_user_id") or "")[:22]
            dn = (e.get("display_name") or "")[:25]
            rn = (e.get("real_name") or "")[:30]
            role = (e.get("role") or "")[:26]
            notes = (e.get("notes") or "")[:60]
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
