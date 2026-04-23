"""System prompts for the intent classifier.

The system prompt is static; it is a good candidate for prompt caching (the
Anthropic SDK applies a `cache_control` breakpoint on system blocks).
"""

SYSTEM_PROMPT = """\
You are an intent extraction engine for a Slack-based task manager.

Given a Slack source message plus surrounding context, determine whether the
author intended to create or update a TASK or MEETING, or whether no action is
needed.

Return ONLY one of the following intents:
- "create_task"     — the message describes a new actionable task
- "create_meeting"  — the message proposes a new meeting / call / sync
- "update_task"     — the message modifies an existing task (reschedule, reassign, close)
- "update_meeting"  — the message modifies an existing meeting
- "no_action"       — chat, question, observation, nothing to create

Rules:
1. Be conservative. When ambiguous, emit "no_action" with a low confidence.
2. Confidence must be in [0, 1]. Reserve >= 0.75 for clear, explicit cases.
3. For "create_task", extract title (imperative), description, owner_display_name,
   priority ("low"|"medium"|"high"|"urgent"), and due_date (YYYY-MM-DD).
4. For "create_meeting", extract title, notes, participants (list), datetime_at
   (ISO 8601 with timezone offset when known), timezone.
5. Resolve relative dates ("tomorrow", "завтра", "до пятницы") against the
   provided current_date. If unknown, leave the field null — do not guess.
6. Respond with a single JSON object matching the provided schema.
"""


def build_user_prompt(
    *,
    source_text: str,
    context_messages: list[dict],
    invocation_type: str,
    current_date: str,
) -> str:
    lines = [
        f"current_date: {current_date}",
        f"invocation_type: {invocation_type}",
        "",
        "context (oldest first):",
    ]
    for m in context_messages:
        user = m.get("user") or "unknown"
        text = (m.get("text") or "").replace("\n", " ").strip()
        ts = m.get("ts") or ""
        lines.append(f"- [{ts}] {user}: {text}")
    lines.append("")
    lines.append("source_message:")
    lines.append(source_text)
    return "\n".join(lines)
