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
    # «([label](url))» — markdown link inside parens
    (
        re.compile(r"\(\[([^\]\n]+)\]\(\s*(https?://[^)\s]+)\)\)"),
        "md_paren",
    ),
    # «[label](url)» — bare markdown link
    (
        re.compile(r"\[([^\]\n]+)\]\(\s*(https?://[^)\s]+)\)"),
        "md_inline",
    ),
    # «([host.tld])» — bracketed bare-host (Responses API native)
    (
        re.compile(
            r"\(\[([a-zA-Z0-9._\-/]+\.[a-z]{2,}[a-zA-Z0-9._\-/]*)\]\)"
        ),
        "bare_host",
    ),
    # «(host.tld)» — plain-parens bare host (LLM often emits
    # citations like «...professional at CDIB (tw.linkedin.com).»).
    # Require lowercase letter at start so we don't grab
    # parentheticals like `(US$20 billion)` or `(陳衍均)`.
    (
        re.compile(
            r"\(([a-z][a-z0-9\-]*(?:\.[a-z][a-z0-9\-]*)+)\)"
        ),
        "bare_host_no_brackets",
    ),
)

# Two private-use chars wrap each placeholder token so neither
# `_slack_safe` nor `_shorten` can break it mid-cut.
_PH_OPEN = ""
_PH_CLOSE = ""


def _strip_anchor_fragment(url: str) -> str:
    """Drop the `#:~:text=…` highlight anchor — those URLs are
    correct but ugly; we want plain `https://host/path` in the
    visible hyperlink."""
    return url.split("#", 1)[0]


def _linkify_citations(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Replace citation markers with private-use placeholder tokens
    and return both the rewritten text and a list of
    ``(url, label)`` tuples. The caller must run `_slack_safe` on
    the placeholder text and only then call `_expand_links` — that
    way safer-string mangling doesn't touch the link markup itself.

    Operator-pinned 2026-05-18: «ну ты же умеешь делать гиперссылки
    как с задачами» — citations become clickable Slack hyperlinks
    instead of being stripped silently.
    """
    if not text or not isinstance(text, str):
        return text, []
    placeholders: list[tuple[str, str]] = []

    def _emit(url: str, label: str) -> str:
        placeholders.append((_strip_anchor_fragment(url), label))
        return f"{_PH_OPEN}{len(placeholders) - 1}{_PH_CLOSE}"

    for pattern, kind in _CITATION_PATTERNS:
        def _sub(m: re.Match[str], _kind: str = kind) -> str:
            if _kind in ("bare_host", "bare_host_no_brackets"):
                host = m.group(1).rstrip("/").lstrip("/")
                return _emit(f"https://{host}", host)
            label, url = m.group(1), m.group(2)
            return _emit(url, label)

        text = pattern.sub(_sub, text)
    # Glue dangling punctuation that lived right after the marker.
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text, placeholders


def _expand_links(
    text: str, placeholders: list[tuple[str, str]],
) -> str:
    if not placeholders:
        return text

    def _sub(m: re.Match[str]) -> str:
        idx = int(m.group(1))
        if idx >= len(placeholders):
            return ""
        url, label = placeholders[idx]
        # Label must NOT contain literal `<` `>` `|`; reuse the
        # same swap rules as `_slack_safe`.
        safe_label = (
            label.replace("<", "‹").replace(">", "›").replace("|", "/")
        )
        return f"<{url}|{safe_label}>"

    return re.sub(
        rf"{_PH_OPEN}(\d+){_PH_CLOSE}", _sub, text,
    )


def _render_gist(raw: str, *, max_chars: int) -> str:
    """End-to-end gist pipeline: linkify citations → slack-safe →
    soft-cut → expand link placeholders. Apply once per
    free-text field (org overview, person profile)."""
    rewritten, placeholders = _linkify_citations(raw or "")
    safe = _slack_safe(rewritten)
    shortened = _shorten(safe, max_chars=max_chars)
    return _expand_links(shortened, placeholders)


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
        gist = _render_gist(
            org_brief.get("gist") or "", max_chars=_ORG_GIST_MAX,
        )
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
    gist = _render_gist(
        person.get("gist") or "", max_chars=_PERSON_GIST_MAX,
    )
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
        gist = _render_gist(org_brief.get("gist") or "", max_chars=600)
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
