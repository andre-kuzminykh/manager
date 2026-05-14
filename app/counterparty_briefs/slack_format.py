"""FR-CR-05-168 — Slack mrkdwn renderer (grouped per-event DM).

Operator-pinned format:

    *Новая встреча DD/MM HH:MM: <Event Title>*

    Справки готовы:
    • <doc-url|🏢 <Org Name>>
    • <doc-url|👤 <Person Name> — <Role>>
    • <doc-url|👤 <Person Name> — <Role>>

Slack-mrkdwn `<>` `|` characters in name/title are swapped to
`‹›` `/` so the link parser doesn't break (same rule as
`slack_mirror._link_sub`).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


_SLACK_TEXT_CAP = 2900


def _slack_safe(text: str) -> str:
    return (
        (text or "")
        .replace("&amp;", "&")
        .replace("&lt;", "‹")
        .replace("&gt;", "›")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&#x27;", "'")
        .replace("&apos;", "'")
        .replace("<", "‹")
        .replace(">", "›")
        .replace("|", "/")
    )


def _ddmm_hhmm(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%d/%m %H:%M")


def render_event_briefs_slack_text(
    *,
    event_title: str,
    scheduled_at: datetime,
    org_brief: dict[str, Any] | None,
    person_briefs: list[dict[str, Any]],
) -> str:
    """Build the single-message Slack body for an event with
    multiple briefs."""
    safe_title = _slack_safe(event_title)
    lines: list[str] = [
        f"*Новая встреча {_ddmm_hhmm(scheduled_at)}: {safe_title}*",
        "",
        "Справки готовы:",
    ]

    if org_brief and org_brief.get("doc_url"):
        url = org_brief["doc_url"]
        label = _slack_safe(org_brief.get("display_name") or "Org")
        lines.append(f"• <{url}|🏢 {label}>")

    # Cap at 10 person briefs to stay under the 3000-char Slack
    # text limit; remainder summarised at the bottom.
    rendered_persons = (person_briefs or [])[:10]
    for pb in rendered_persons:
        url = pb.get("doc_url")
        if not url:
            label = _slack_safe(pb.get("display_name") or "—")
            note = pb.get("note") or "research_failed"
            lines.append(f"• 👤 {label} — N/A ({note})")
            continue
        label = _slack_safe(pb.get("display_name") or "—")
        role = (pb.get("role") or "").strip()
        suffix = f" — {_slack_safe(role)}" if role else ""
        lines.append(f"• <{url}|👤 {label}{suffix}>")

    overflow = len(person_briefs) - len(rendered_persons)
    if overflow > 0:
        lines.append(f"…ещё {overflow} в Google Doc")

    out = "\n".join(lines)
    if len(out) > _SLACK_TEXT_CAP:
        out = out[:_SLACK_TEXT_CAP - 3].rstrip() + "\n…"
    return out


__all__ = ["render_event_briefs_slack_text"]
