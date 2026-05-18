"""FR-CR-05-168 — Slack mrkdwn renderer (grouped per-event DM).

Operator-pinned format (2026-05-18, final):

    *Новая встреча DD/MM HH:MM: <Event Title>*

    <doc-url|🏢 *<Org Name>*>
    <prose with citations attached as hyperlinks to preceding words>

    👇 Информация о N контактах — в треде ниже

    (thread replies, one per person)
    <doc-url|👤 *<Person Name>* — <Role>>
    <prose with citations as word-anchored hyperlinks>

Citations: `(host.tld)` / `([host.tld])` / `[label](url)` markers
are removed from prose; the PRECEDING WORD becomes a clickable
Slack hyperlink to the cited source. Repeated citations for the
same URL keep only the FIRST anchor and silently drop subsequent
occurrences. This way the gist reads like clean prose with
occasional clickable highlights rather than `(host.tld)` noise.
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


# Citation patterns + URL extractors. Order matters: longer /
# more-specific forms first so we don't consume a substring of a
# bigger marker by accident.
_CITATION_EXTRACTORS = (
    # «([label](url))» — markdown link inside parens
    (
        re.compile(r"\(\[([^\]\n]+)\]\(\s*(https?://[^)\s]+)\)\)"),
        lambda m: m.group(2),
    ),
    # «[label](url)» — bare markdown link
    (
        re.compile(r"\[([^\]\n]+)\]\(\s*(https?://[^)\s]+)\)"),
        lambda m: m.group(2),
    ),
    # «([host.tld])» — bracketed bare-host (Responses API native)
    (
        re.compile(
            r"\(\[([a-zA-Z0-9._\-/]+\.[a-z]{2,}[a-zA-Z0-9._\-/]*)\]\)"
        ),
        lambda m: f"https://{m.group(1).strip('/')}",
    ),
    # «(host.tld)» — plain-parens bare host.
    (
        re.compile(r"\(([a-z][a-z0-9\-]*(?:\.[a-z][a-z0-9\-]*)+)\)"),
        lambda m: f"https://{m.group(1)}",
    ),
)


# Placeholder encoding: a sentinel control byte + a Private-Use-Area
# char that carries the index. PUA chars are NOT word chars under
# `re.UNICODE`, so the word-anchor regex skips placeholders when
# looking for the word before a citation marker.
#
#   CITE_SENTINEL + chr(0xE000 + idx)   ← pass-1 citation marker
#   LINK_SENTINEL + chr(0xE100 + idx)   ← pass-2 hyperlink marker

_CITE_SENTINEL = "\x01"
_LINK_SENTINEL = "\x02"

_CITE_MARKER_RE = re.compile(_CITE_SENTINEL + r"([-])")
_LINK_MARKER_RE = re.compile(_LINK_SENTINEL + r"([-])")


def _make_cite_marker(idx: int) -> str:
    return _CITE_SENTINEL + chr(0xE000 + idx)


def _make_link_marker(idx: int) -> str:
    return _LINK_SENTINEL + chr(0xE100 + idx)


def _strip_anchor_fragment(url: str) -> str:
    """Drop `#:~:text=…` — those URLs are correct but ugly when
    shown as the visible hyperlink label."""
    return url.split("#", 1)[0]


# Word-anchor: the LAST word in a string. Allows in-word
# apostrophes (`don't`), hyphens (`Asia-Pacific`), and dots
# between alphanumeric segments (`WEB.DE`, `Mr.Smith`,
# `J.Y. Koo` — but only when the dot is IMMEDIATELY followed by
# a word char, so it doesn't glue across sentence boundaries
# like «experience. CDIB»). TAIL captures any non-word
# punctuation/whitespace between the word and the citation
# marker, so the rewrite preserves spacing.
_WORD_TAIL_RE = re.compile(
    r"(\w+(?:[’'\.\-]\w+)*)([^\w]*)$",
    flags=re.UNICODE,
)


def _linkify_citations(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Operator-pinned 2026-05-18 (final revision): citation
    markers attach as Slack hyperlinks to the PRECEDING WORD; the
    visible URL/host text is removed. Repeated citations for the
    same URL after the first anchor are silently dropped.

    Two-pass algorithm:

      1. Replace every citation marker (any of 4 forms) with a
         CITE placeholder (sentinel + PUA char). URL stored in
         a side list, indexed by the PUA codepoint.
      2. Walk CITE placeholders in REVERSE order so position
         rewrites don't shift earlier markers. For each:
           * if URL already linked once in the prose → drop the
             CITE marker (and the whitespace immediately before
             it).
           * else find the word IMMEDIATELY BEFORE the marker
             (skipping punctuation / spaces / other CITE markers)
             and replace it with a LINK placeholder. The
             intermediate tail (punctuation + spaces) is kept.
           * if no preceding word exists → drop CITE.

    Returns ``(rewritten_text, [(url, anchor_word), …])``. Caller
    runs ``_slack_safe`` on the rewritten text, then
    ``_expand_links`` to turn LINK placeholders into Slack
    mrkdwn ``<url|word>`` links.
    """
    if not text or not isinstance(text, str):
        return text, []

    citation_urls: list[str] = []

    def _replace_cite(m: "re.Match[str]", extractor) -> str:
        citation_urls.append(_strip_anchor_fragment(extractor(m)))
        return _make_cite_marker(len(citation_urls) - 1)

    for pattern, extractor in _CITATION_EXTRACTORS:
        text = pattern.sub(
            lambda m, _e=extractor: _replace_cite(m, _e), text,
        )

    link_placeholders: list[tuple[str, str]] = []
    seen_urls: set[str] = set()

    # Walk in reverse, re-scanning after each rewrite so positions
    # stay current (earlier-position rewrites don't shift later
    # markers' positions, but later-marker rewrites can extend the
    # text and leave earlier markers AT MODIFIED INDICES — we
    # avoid that complication by re-finding markers each step).
    while True:
        matches = list(_CITE_MARKER_RE.finditer(text))
        if not matches:
            break
        m = matches[-1]
        cite_start, cite_end = m.start(), m.end()
        url = citation_urls[ord(m.group(1)) - 0xE000]
        before = text[:cite_start]

        if url in seen_urls:
            ws_start = cite_start
            while ws_start > 0 and text[ws_start - 1] == " ":
                ws_start -= 1
            text = text[:ws_start] + text[cite_end:]
            continue

        wm = _WORD_TAIL_RE.search(before)
        if not wm:
            ws_start = cite_start
            while ws_start > 0 and text[ws_start - 1] == " ":
                ws_start -= 1
            text = text[:ws_start] + text[cite_end:]
            continue

        word, tail = wm.group(1), wm.group(2)
        word_start = wm.start(1)
        seen_urls.add(url)
        link_placeholders.append((url, word))
        link_idx = len(link_placeholders) - 1
        text = (
            text[:word_start]
            + _make_link_marker(link_idx)
            + tail
            + text[cite_end:]
        )

    # Clean up double-spaces / dangling punctuation left after
    # marker removal.
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text, link_placeholders


def _expand_links(
    text: str, placeholders: list[tuple[str, str]],
) -> str:
    if not placeholders:
        return text

    def _sub(m: "re.Match[str]") -> str:
        idx = ord(m.group(1)) - 0xE100
        if idx < 0 or idx >= len(placeholders):
            return ""
        url, label = placeholders[idx]
        safe_label = (
            label.replace("<", "‹").replace(">", "›").replace("|", "/")
        )
        return f"<{url}|{safe_label}>"

    return _LINK_MARKER_RE.sub(_sub, text)


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


def _render_gist(raw: str, *, max_chars: int) -> str:
    """End-to-end gist pipeline: linkify citations → slack-safe →
    soft-cut → expand link placeholders. Apply once per
    free-text field (org overview, person profile)."""
    rewritten, placeholders = _linkify_citations(raw or "")
    safe = _slack_safe(rewritten)
    shortened = _shorten(safe, max_chars=max_chars)
    return _expand_links(shortened, placeholders)


def render_org_top_message(
    *,
    event_title: str,
    scheduled_at: datetime,
    org_brief: dict[str, Any] | None,
    person_count: int,
) -> str:
    """Top-of-thread message: header + org info hyperlinked to the
    Doc + pointer to person briefs in the thread below."""
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
