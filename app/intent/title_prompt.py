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
                tight (<= 80 chars). Strip wrappers like "надо",
                "please". Use the imperative form
                ("подготовить питчдек", "prepare pitch-deck").
- description:  any supplementary detail present in the source
                message — references, numbers, sub-items, rationale.
                null when the title already captures everything.
- priority:     one of "low" | "medium" | "high" | "urgent".
                Default "medium". Use "urgent" only when the author
                says so ("срочно", "ASAP", "blocker", "сегодня же"),
                "high" for "важно"/"important".

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
