"""FR-CR-05-168 — Slack mrkdwn renderer (grouped per-event DM).

Operator-pinned format (2026-05-18):

    *Новая встреча DD/MM HH:MM: <Event Title>*

    <doc-url|🏢 *<Org Name>*>
    <long gist — up to 1500 chars>

    👇 Информация о N контактах — в треде ниже

    (thread replies, one per person)
    <doc-url|👤 *<Person Name>* — <Role>>
    <long gist — up to 1200 chars>

Slack-mrkdwn `<>` `|` characters in name/title/gist are swapped
to `‹›` `/` so the link parser doesn't break (same rule as
`slack_mirror._link_sub`). Only the OUTER `<url|label>` markup
uses literal `<`, `|`, `>` — its inner label is pre-escaped.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any


_SLACK_TEXT_CAP = 2900
_ORG_GIST_MAX = 1500
_PERSON_GIST_MAX = 1200


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


_CITATION_PATTERNS = (
    # «([label](url#:~:text=…))» — markdown link inside parens
    re.compile(r"\s*\(\[[^\]]+\]\([^)]*#:~:text=[^)]*\)\)"),
    # «[label](url#:~:text=…)» — markdown link inline
    re.compile(r"\s*\[[^\]]+\]\([^)]*#:~:text=[^)]*\)"),
    # «(host.tld#:~:text=…)» — bare parenthetical
    re.compile(r"\s*\([^()]*#:~:text=[^()]*\)"),
    # «([host.tld](url))» — citation paren with markdown link, no
    # #:~:text= anchor
    re.compile(r"\s*\(\[[^\]\n]+\]\(https?://[^)\s]+\)\)"),
    # «([host.tld])» — bare bracketed citation (Responses API native)
    re.compile(r"\s*\(\[[a-zA-Z0-9._\-/]+\.[a-z]{2,}[a-zA-Z0-9._\-/]*\]\)"),
)


def _strip_citations(text: str) -> str:
    """Render-time scrubber so even pre-strip cached payloads come
    out clean. Mirrors ``research._strip_citations`` exactly so a
    text scrubbed here matches what fresh research would produce.
    """
    if not text or not isinstance(text, str):
        return text
    for pat in _CITATION_PATTERNS:
        text = pat.sub("", text)
    # Glue punctuation back to the preceding word after citations
    # were stripped from before them: «Foo .» → «Foo.».
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    return text


def _ddmm_hhmm(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%d/%m %H:%M")


def _shorten(text: str, max_chars: int = 220) -> str:
    """Soft-cut of a paragraph. Strips, collapses newlines to
    single spaces, hard-caps at max_chars with an ellipsis."""
    s = (text or "").strip().replace("\n", " ")
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 1].rstrip() + "…"


def _slack_link(url: str, label: str) -> str:
    """Build a Slack-mrkdwn `<url|label>` link. ``label`` MUST be
    pre-escaped (no raw `<` `>` `|` chars)."""
    return f"<{url}|{label}>"


def render_org_top_message(
    *,
    event_title: str,
    scheduled_at: datetime,
    org_brief: dict[str, Any] | None,
    person_count: int,
) -> str:
    """Top-of-thread message: header + org info hyperlinked to the
    Doc + pointer to person briefs in the thread below.

    Operator-pinned 2026-05-18: «надо гиперссылки делать и больше
    информации, а о физиках в треде и указание что там»."""
    safe_title = _slack_safe(event_title)
    lines: list[str] = [
        f"*Новая встреча {_ddmm_hhmm(scheduled_at)}: {safe_title}*",
    ]
    if org_brief:
        name = _slack_safe(org_brief.get("display_name") or "Org")
        gist_raw = _strip_citations(org_brief.get("gist") or "")
        gist = _slack_safe(_shorten(gist_raw, max_chars=_ORG_GIST_MAX))
        url = org_brief.get("doc_url") or ""
        lines.append("")
        header_label = f"🏢 *{name}*"
        if url:
            lines.append(_slack_link(url, header_label))
        else:
            lines.append(header_label)
        if gist:
            lines.append(gist)
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
    """One thread reply per person — hyperlinked emoji+name+role
    followed by the gist."""
    name = _slack_safe(person.get("display_name") or "—")
    role = (person.get("role") or "").strip()
    gist_raw = _strip_citations(person.get("gist") or "")
    gist = _slack_safe(_shorten(gist_raw, max_chars=_PERSON_GIST_MAX))
    url = person.get("doc_url")
    note = person.get("note") or ""
    header_label = f"👤 *{name}*"
    if role:
        header_label += f" — {_slack_safe(role)}"
    if not url:
        return f"{header_label} — N/A ({note or 'research_failed'})"
    lines: list[str] = [_slack_link(url, header_label)]
    if gist:
        lines.append(gist)
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
        gist_raw = _strip_citations(org_brief.get("gist") or "")
        gist = _slack_safe(_shorten(gist_raw))
        url = org_brief.get("doc_url") or ""
        header_label = f"🏢 *{name}*"
        lines.append("")
        if url:
            lines.append(_slack_link(url, header_label))
        else:
            lines.append(header_label)
        if gist:
            lines.append(gist)
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
