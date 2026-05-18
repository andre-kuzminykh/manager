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


def render_org_top_message(
    *,
    event_title: str,
    scheduled_at: datetime,
    org_brief: dict[str, Any] | None,
    person_count: int,
) -> str:
    """Top-of-thread message: header + org info + Doc link +
    pointer to person briefs in the thread below.

    Operator-pinned 2026-05-18: «сделать так, что по сути без
    лишней воды но подробно пишется информация о контрагенте
    юрике детально, гиперссылка на док, ниже в самом треде
    информация о физиках».
    """
    safe_title = _slack_safe(event_title)
    lines: list[str] = [
        f"*Новая встреча {_ddmm_hhmm(scheduled_at)}: {safe_title}*",
    ]
    if org_brief:
        name = _slack_safe(org_brief.get("display_name") or "Org")
        gist = _slack_safe(_shorten(
            org_brief.get("gist") or "", max_chars=600
        ))
        url = org_brief.get("doc_url") or ""
        lines.append("")
        lines.append(f"🏢 *{name}*")
        if gist:
            lines.append(gist)
        if url:
            lines.append(url)
    if person_count > 0:
        lines.append("")
        word = "контактах" if person_count > 1 else "контакте"
        lines.append(
            f"👇 Информация о {person_count} {word} — в треде ниже"
        )
    out = "\n".join(lines)
    if len(out) > _SLACK_TEXT_CAP:
        out = out[:_SLACK_TEXT_CAP - 3].rstrip() + "\n…"
    return out


def render_person_thread_reply(*, person: dict[str, Any]) -> str:
    """One thread reply per person.

    Shape:
        👤 *<Name>* — <Role>
        <one-line gist>
        <Doc URL>
    """
    name = _slack_safe(person.get("display_name") or "—")
    role = (person.get("role") or "").strip()
    gist = _slack_safe(_shorten(person.get("gist") or "", max_chars=600))
    url = person.get("doc_url")
    note = person.get("note") or ""
    lines: list[str] = []
    head = f"👤 *{name}*"
    if role:
        head += f" — {_slack_safe(role)}"
    if not url:
        head += f" — N/A ({note or 'research_failed'})"
        return head
    lines.append(head)
    if gist:
        lines.append(gist)
    lines.append(url)
    out = "\n".join(lines)
    if len(out) > _SLACK_TEXT_CAP:
        out = out[:_SLACK_TEXT_CAP - 3].rstrip() + "\n…"
    return out


# -- backward-compatible legacy renderer (used by current tests &
#    one-shot CLI when threading is not desirable) ----------------

def render_event_briefs_slack_text(
    *,
    event_title: str,
    scheduled_at: datetime,
    org_brief: dict[str, Any] | None,
    person_briefs: list[dict[str, Any]],
) -> str:
    """Single-message flat layout (legacy)."""
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
        lines.append("")
        lines.append(render_person_thread_reply(person=pb))
    overflow = len(person_briefs) - len(rendered_persons)
    if overflow > 0:
        lines.append("")
        lines.append(f"…ещё {overflow} в Google Doc")
    out = "\n".join(lines)
    if len(out) > _SLACK_TEXT_CAP:
        out = out[:_SLACK_TEXT_CAP - 3].rstrip() + "\n…"
    return out


__all__ = [
    "render_event_briefs_slack_text",
    "render_org_top_message",
    "render_person_thread_reply",
]
