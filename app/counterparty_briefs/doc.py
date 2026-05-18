"""FR-CR-05-168 — Google Doc body renderers.

Two builders:
  * ``build_org_doc_body`` — org-level: Overview, Leadership,
    Portfolio, Recent Activity, Past meetings c нами, Open tasks.
  * ``build_person_doc_body`` — operator-pinned §6.2 person
    format with photo at top + Personal Info + DD/MM Саммари +
    To-Do + Profile Overview + Current/Previous Positions +
    Investment Highlights + Investments + Exits + Achievements +
    Honors + Education + Publications + Skills + Languages.

Plus ``build_doc_title(kind, display_name, scheduled_at)`` —
operator-pinned «DD/MM - Brief: <name> [(org)]» format.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def markdown_to_html(md: str) -> str:
    """Tiny Markdown → HTML converter used for Brief Docs.

    Handles only what we emit: H1/H2/H3, bold, italic, list items,
    plain paragraphs, fenced code blocks, GitHub-flavoured tables,
    bare URLs (autolinked) and `[label](url)` hyperlinks. Anything
    we don't recognise is preserved as a paragraph. No external
    deps so the runtime image stays minimal.
    """
    import html
    import re

    def _esc(s: str) -> str:
        return html.escape(s, quote=False)

    def _inline(s: str) -> str:
        # `([host.tld])` — bare bracketed citation marker
        # (Responses API native). Convert to a clickable <a>
        # pointing at `https://host.tld`. Operator-pinned
        # 2026-05-18: «ну ты же умеешь делать гиперссылки».
        s = re.sub(
            r"\(\[([a-zA-Z0-9._\-/]+\.[a-z]{2,}[a-zA-Z0-9._\-/]*)\]\)",
            lambda m: (
                f'(<a href="https://{_esc(m.group(1).lstrip("/").rstrip("/"))}">'
                f'{_esc(m.group(1))}</a>)'
            ),
            s,
        )
        # `[label](url)` — wrap in <a>. Run BEFORE bare-url
        # autolinking so we don't double-wrap.
        s = re.sub(
            r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)",
            lambda m: f'<a href="{_esc(m.group(2))}">{_esc(m.group(1))}</a>',
            s,
        )
        # Bare URLs → <a>
        def _bare(m: "re.Match[str]") -> str:
            url = m.group(1)
            return f'<a href="{_esc(url)}">{_esc(url)}</a>'
        s = re.sub(
            r"(?<!href=\")(https?://[^\s<>)]+)",
            _bare,
            s,
        )
        # **bold**
        s = re.sub(r"\*\*([^*\n]+?)\*\*", r"<strong>\1</strong>", s)
        # _italic_ (only when wrapped by spaces / start / end)
        s = re.sub(
            r"(^|\W)_([^_\n]+?)_(?=\W|$)",
            r"\1<em>\2</em>",
            s,
        )
        return s

    out: list[str] = []
    in_list = False
    in_table = False
    table_rows: list[list[str]] = []

    def _flush_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    def _flush_table() -> None:
        nonlocal in_table, table_rows
        if not in_table:
            return
        if table_rows:
            head = table_rows[0]
            body_rows = [r for r in table_rows[1:] if r and not all(
                set(c.strip()) <= {"-", ":", " "} for c in r
            )]
            out.append("<table border=\"1\" cellspacing=\"0\" cellpadding=\"4\">")
            out.append("<thead><tr>" + "".join(
                f"<th>{_inline(_esc(h.strip()))}</th>" for h in head
            ) + "</tr></thead>")
            out.append("<tbody>")
            for row in body_rows:
                out.append("<tr>" + "".join(
                    f"<td>{_inline(_esc(c.strip()))}</td>" for c in row
                ) + "</tr>")
            out.append("</tbody></table>")
        table_rows = []
        in_table = False

    for raw_line in (md or "").splitlines():
        line = raw_line.rstrip("\r")
        stripped = line.strip()

        # Markdown table row?
        if stripped.startswith("|") and stripped.endswith("|") and "|" in stripped[1:-1]:
            _flush_list()
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            table_rows.append(cells)
            in_table = True
            continue
        if in_table:
            _flush_table()

        if not stripped:
            _flush_list()
            out.append("")
            continue
        if stripped.startswith("# "):
            _flush_list()
            out.append(f"<h1>{_inline(_esc(stripped[2:].strip()))}</h1>")
            continue
        if stripped.startswith("## "):
            _flush_list()
            out.append(f"<h2>{_inline(_esc(stripped[3:].strip()))}</h2>")
            continue
        if stripped.startswith("### "):
            _flush_list()
            out.append(f"<h3>{_inline(_esc(stripped[4:].strip()))}</h3>")
            continue
        if stripped.startswith("- ") or stripped.startswith("* "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(_esc(stripped[2:].strip()))}</li>")
            continue
        # Plain bare URL on its own line — likely the hero photo.
        if re.match(r"^https?://\S+$", stripped):
            _flush_list()
            out.append(
                f'<p><img src="{_esc(stripped)}" alt="" /></p>'
            )
            continue

        _flush_list()
        out.append(f"<p>{_inline(_esc(stripped))}</p>")

    _flush_list()
    _flush_table()

    return (
        "<!DOCTYPE html><html><head>"
        '<meta charset="utf-8"></head><body>'
        + "\n".join(out)
        + "</body></html>"
    )


def build_doc_title(
    *,
    kind: str,
    display_name: str,
    scheduled_at: datetime,
) -> str:
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
    dd_mm = scheduled_at.strftime("%d/%m")
    if kind == "org":
        return f"{dd_mm} - Brief: {display_name} (org)"
    return f"{dd_mm} - Brief: {display_name}"


def _ddmm_from_iso(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00")).date()
        return d.strftime("%d/%m")
    except ValueError:
        return ""


def build_org_doc_body(
    *,
    org_name: str,
    context: Any,  # CounterpartyContext | None
    research: Any,  # OrgResearch | None
) -> str:
    """Org Doc — sections: Overview / Leadership / Portfolio /
    Recent Activity / Past meetings c нами / Open tasks."""
    lines: list[str] = []
    name = (
        (research and research.name)
        or (research and research.official_name)
        or org_name
        or "Counterparty"
    )
    lines.append(f"# {name}")
    if research and research.headquarters:
        lines.append(f"_HQ: {research.headquarters}_")
    if research and research.type:
        lines.append(f"_Type: {research.type}_")
    if research and research.website:
        lines.append(f"Website: [{research.website}]({research.website})")

    lines.append("")
    lines.append("## Overview")
    lines.append((research and research.overview_paragraph) or "N/A")

    lines.append("")
    lines.append("## Leadership")
    if research and research.leadership:
        for ld in research.leadership[:20]:
            name_ = (ld.get("name") or "").strip()
            role = (ld.get("role") or "").strip()
            url = (ld.get("linkedin_url") or ld.get("evidence_url") or "").strip()
            label = name_ or "—"
            if url:
                lines.append(
                    f"- [{label}]({url})"
                    f"{(' — ' + role) if role else ''}"
                )
            else:
                lines.append(
                    f"- {label}{(' — ' + role) if role else ''}"
                )
    else:
        lines.append("N/A")

    lines.append("")
    lines.append("## Portfolio")
    if research and research.portfolio_highlights:
        for p in research.portfolio_highlights[:20]:
            lines.append(
                f"- {p.get('name','?')}"
                f" — {p.get('deal_size','?')} ({p.get('year','?')})"
            )
    else:
        lines.append("N/A")

    lines.append("")
    lines.append("## Recent Activity")
    if research and research.recent_news:
        for n in research.recent_news[:10]:
            date = n.get("date") or ""
            title = (n.get("title") or "").strip() or "—"
            url = (n.get("url") or "").strip()
            if url:
                lines.append(f"- {date} — [{title}]({url})")
            else:
                lines.append(f"- {date} — {title}")
    else:
        lines.append("N/A")

    lines.append("")
    lines.append("## Past meetings c нами")
    past = list(getattr(context, "past_recordings", None) or [])
    if past:
        for r in past[:10]:
            dd_mm = _ddmm_from_iso(r.get("meeting_date"))
            title = (r.get("title") or "").strip() or "—"
            doc_url = (r.get("google_doc_url") or "").strip()
            if doc_url:
                lines.append(f"- {dd_mm} — [{title}]({doc_url})")
            else:
                lines.append(f"- {dd_mm} — {title}")
    else:
        lines.append("N/A")

    lines.append("")
    lines.append("## Open tasks")
    open_tasks = list(getattr(context, "open_tasks", None) or [])
    if open_tasks:
        for t in open_tasks[:20]:
            title = (t.get("title") or "").strip()
            owner = (t.get("owner_display_name") or "").strip()
            due = t.get("due_date") or ""
            tail = " — " + " • ".join(
                p for p in (owner, due, t.get("status")) if p
            )
            lines.append(f"- {title}{tail}")
    else:
        lines.append("N/A")

    return "\n".join(lines) + "\n"


def build_person_doc_body(
    *,
    beneficiary: Any,  # BeneficiaryCandidate | None
    context: Any,  # CounterpartyContext | None
    research: Any,  # PersonResearch | None
) -> str:
    """Person Doc — operator-pinned §6.2 schema."""
    lines: list[str] = []

    # Hero photo (bare URL — Google Docs auto-embeds).
    photo = (
        research and research.photo_url
        if research is not None else None
    )
    if photo:
        lines.append(photo)
        lines.append("")

    pi = (research and research.personal_information) or {}
    name = (
        pi.get("name")
        or (beneficiary and beneficiary.person_name)
        or "Counterparty"
    )
    role = pi.get("role") or (beneficiary and beneficiary.person_role) or ""
    company = pi.get("company") or ""
    header_role = f" - {role}" if role else ""
    header_company = f" at {company}" if company else ""
    lines.append(f"# {name}{header_role}{header_company}")

    lines.append("")
    lines.append("## Personal Information")
    lines.append(f"Name: {pi.get('name') or name}")
    if pi.get("role"):
        lines.append(f"Role: {pi.get('role')}")
    if pi.get("location"):
        lines.append(f"Location: {pi.get('location')}")
    if pi.get("linkedin_url"):
        lines.append(f"LinkedIn: [{pi['linkedin_url']}]({pi['linkedin_url']})")
    if pi.get("company_website"):
        lines.append(
            f"Company Website: [{pi['company_website']}]({pi['company_website']})"
        )
    emails = pi.get("emails") or []
    phone = pi.get("phone") or ""
    if emails or phone:
        lines.append("Contacts:")
        if emails:
            lines.append(f"  Email: {' / '.join(emails)}")
        lines.append(f"  Tel.: {phone or 'n/a'}")

    # DD/MM Саммари — newest past recording's short_summary
    past = list(getattr(context, "past_recordings", None) or [])
    if past:
        newest = past[0]
        dd_mm = _ddmm_from_iso(newest.get("meeting_date"))
        summary = (newest.get("short_summary") or "").strip()
        if summary:
            lines.append("")
            lines.append(f"## {dd_mm} Саммари")
            lines.append(summary)

    lines.append("")
    lines.append("## To-Do")
    open_tasks = list(getattr(context, "open_tasks", None) or [])
    if open_tasks:
        for t in open_tasks[:20]:
            title = (t.get("title") or "").strip()
            lines.append(f"- {title}")
    else:
        lines.append("N/A")

    lines.append("")
    lines.append("## Profile Overview")
    lines.append((research and research.profile_overview) or "N/A")

    lines.append("")
    lines.append("## Current Position")
    current = (research and research.current_positions) or []
    if current:
        for cp in current:
            lines.append(f"Role: {cp.get('role','?')}")
            lines.append(f"Company: {cp.get('company','?')}")
            if cp.get("duration"):
                lines.append(f"Duration: {cp['duration']}")
            if cp.get("focus"):
                lines.append(f"Focus: {cp['focus']}")
            lines.append("")
    else:
        lines.append("N/A")

    lines.append("## Previous Positions")
    prev = (research and research.previous_positions) or []
    if prev:
        for pp in prev:
            lines.append(f"Role: {pp.get('role','?')}")
            lines.append(f"Company: {pp.get('company','?')}")
            if pp.get("duration"):
                lines.append(f"Duration: {pp['duration']}")
            if pp.get("focus"):
                lines.append(f"Focus: {pp['focus']}")
            lines.append("")
    else:
        lines.append("N/A")

    lines.append("## Investment Highlights")
    ih = (research and research.investment_highlights) or {}
    if ih:
        for k in (
            "entity_types", "investor_type", "investor_status",
            "total_investments", "active_portfolio", "exits",
            "median_round_amount", "median_valuation",
            "firm_wide_investments", "investment_preferences",
        ):
            v = ih.get(k)
            if v:
                lines.append(f"{k.replace('_', ' ').title()}: {v}")
    else:
        lines.append("N/A")

    lines.append("")
    lines.append("## Investments")
    invs = (research and research.investments) or []
    if invs:
        lines.append("| Company | Deal Date | Deal Type | Deal Size | Stage | Industry |")
        lines.append("|---|---|---|---|---|---|")
        for inv in invs:
            lines.append(
                "| {co} | {dd} | {dt} | {ds} | {st} | {ind} |".format(
                    co=inv.get("company", "?"),
                    dd=inv.get("deal_date", "?"),
                    dt=inv.get("deal_type", "?"),
                    ds=inv.get("deal_size", "?"),
                    st=inv.get("company_stage", "?"),
                    ind=inv.get("industry", "?"),
                )
            )
    else:
        lines.append("N/A")

    lines.append("")
    lines.append("## Exits")
    lines.append((research and research.exits) or "N/A")

    lines.append("")
    lines.append("## Achievements")
    achs = (research and research.achievements) or []
    if achs:
        for a in achs:
            lines.append(f"- {a}")
    else:
        lines.append("N/A")

    lines.append("")
    lines.append("## Honors & Awards")
    hon = (research and research.honors_awards) or []
    if hon:
        for h in hon:
            lines.append(f"- {h}")
    else:
        lines.append("N/A")

    lines.append("")
    lines.append("## Education")
    edu = (research and research.education) or []
    if edu:
        for e in edu:
            lines.append(
                f"- {e.get('institution','?')} — "
                f"{e.get('degree','?')} ({e.get('years','?')})"
            )
    else:
        lines.append("N/A")

    lines.append("")
    lines.append("## Publications")
    pubs = (research and research.publications) or []
    if pubs:
        for p in pubs:
            if isinstance(p, dict):
                title_ = (p.get("title") or "—").strip() or "—"
                url_ = (p.get("url") or "").strip()
                if url_:
                    lines.append(f"- [{title_}]({url_})")
                else:
                    lines.append(f"- {title_}")
            else:
                lines.append(f"- {p}")
    else:
        lines.append("N/A")

    lines.append("")
    lines.append("## Skills")
    skills = (research and research.skills) or []
    if skills:
        lines.append(", ".join(skills))
    else:
        lines.append("N/A")

    lines.append("")
    lines.append("## Languages")
    langs = (research and research.languages) or []
    if langs:
        for ln in langs:
            if isinstance(ln, dict):
                lines.append(
                    f"- {ln.get('language','?')} — {ln.get('level','?')}"
                )
            else:
                lines.append(f"- {ln}")
    else:
        lines.append("N/A")

    return "\n".join(lines) + "\n"


__all__ = ["build_doc_title", "build_org_doc_body", "build_person_doc_body"]
