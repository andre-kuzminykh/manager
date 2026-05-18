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


def _shorten(text: str, max_chars: int = 220) -> str:
    """One-line cut of an overview paragraph. Trim trailing
    whitespace, collapse newlines, hard-cap at max_chars with an
    ellipsis."""
    s = (text or "").strip().replace("\n", " ")
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 1].rstrip() + "…"


def render_event_briefs_slack_text(
    *,
    event_title: str,
    scheduled_at: datetime,
    org_brief: dict[str, Any] | None,
    person_briefs: list[dict[str, Any]],
) -> str:
    """Build the single-message Slack body for an event.

    Operator-pinned 2026-05-18: each line carries a 1-2-sentence
    «суть» pulled from the research payload, so the DM is
    self-contained — no need to click each Doc just to remember
    who the counterparty is.

    Org line shape:
        🏢 <Org name> — <one-line overview>
        <doc-url>

    Person line shape:
        👤 <Name> — <Role>
        <one-line overview>
        <doc-url>
    """
    safe_title = _slack_safe(event_title)
    lines: list[str] = [
        f"*Новая встреча {_ddmm_hhmm(scheduled_at)}: {safe_title}*",
    ]

    if org_brief:
        name = _slack_safe(org_brief.get("display_name") or "Org")
        gist = _slack_safe(_shorten(org_brief.get("gist") or ""))
        url = org_brief.get("doc_url") or ""
        head = f"🏢 *{name}*"
        if gist:
            head += f" — {gist}"
        lines.append("")
        lines.append(head)
        if url:
            lines.append(url)

    rendered_persons = (person_briefs or [])[:10]
    for pb in rendered_persons:
        name = _slack_safe(pb.get("display_name") or "—")
        role = (pb.get("role") or "").strip()
        gist = _slack_safe(_shorten(pb.get("gist") or ""))
        url = pb.get("doc_url")
        note = pb.get("note") or ""
        head = f"👤 *{name}*"
        if role:
            head += f" — {_slack_safe(role)}"
        if not url:
            tail = note or "research_failed"
            head += f" — N/A ({tail})"
            lines.append("")
            lines.append(head)
            continue
        lines.append("")
        lines.append(head)
        if gist:
            lines.append(gist)
        lines.append(url)

    overflow = len(person_briefs) - len(rendered_persons)
    if overflow > 0:
        lines.append("")
        lines.append(f"…ещё {overflow} в Google Doc")

    out = "\n".join(lines)
    if len(out) > _SLACK_TEXT_CAP:
        out = out[:_SLACK_TEXT_CAP - 3].rstrip() + "\n…"
    return out


__all__ = ["render_event_briefs_slack_text"]
