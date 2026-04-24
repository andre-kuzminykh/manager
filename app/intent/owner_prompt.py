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

Return one of:
- slack_user_id   — a Slack user id like "UXXXXXX", when the message
                    addresses a user via <@UXXXXXX>. Copy the id verbatim.
- display_name    — a human name mentioned as the assignee, e.g. "Иван",
                    "Паша", "Pavel". Copy the exact wording without
                    @-prefix.
- null            — no-one is named. Covers: message just describes the
                    work, no handover wording, ambiguous phrases like
                    "надо сделать X" with no target.

Assignment wording clues (Russian + English):
  "на <Имя>", "сделает <Имя>", "делать будет <Имя>",
  "пусть <Имя> сделает", "прошу <Имя>",
  "<Имя>, сделай", "<Имя>, please do X",
  "assign to <Name>", "for <Name>", "can <Name> do X?"

Do NOT pick:
- the message author (their user id appears in context lines as
  "author" — that's attribution, not assignment);
- a bot user (id starting with UBOT… or similar) — bots are never
  assignees;
- a name mentioned only as a reference, e.g. "питчдек для Ивана"
  (Ivan is the AUDIENCE, not the doer).

Prefer slack_user_id when a <@U…> mention is present; fall back to
display_name; otherwise null.

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
) -> str:
    lines: list[str] = []
    if author_user_id:
        lines.append(
            f"source_author: {author_user_id}  (NOT an assignee by default)"
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
