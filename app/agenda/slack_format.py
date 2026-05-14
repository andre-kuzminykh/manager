"""FR-CR-05-165 — Slack mrkdwn renderer for the agenda DM.

FR-CR-05-167 (operator-pinned 2026-05-14): match the style of the
post-meeting summary the operator already gets — no emojis,
numbered task list, recap rendered as prose. Concretely:

    DD/MM - <meeting title> - Повестка

    Участники: <comma-separated names>

    На прошлой встрече: <free text 2-4 sentences>

    К обсуждению:

    1) <task title> - <short description> — <owner> • <DD.MM.YYYY HH:MM> [<status>]
    2) ...

    Подробно: <google doc url>

Status suffix lives at the end of the line and is shown only when
the task is not in the default `todo` state — keeps the «open
items» feel of the operator's pinned format. Long bodies are
trimmed to fit Slack's 3000-char `text` cap; the Doc carries the
full version.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.agenda.compose import AgendaOutput
from app.agenda.service import AgendaCandidate


_STATUS_LABEL = {
    "todo": "",          # default — render blank suffix
    "in_progress": "in_progress",
    "blocked": "blocked",
    "done": "done",
    "cancelled": "cancelled",
}


def _format_attendees(items: list[Any]) -> str:
    """Calendar API returns attendees as
    ``[{email, displayName, responseStatus, organizer?}, ...]``.
    Apps Script proxy returns plain strings. Accept both shapes
    and emit a comma-separated display string. Prefer
    `displayName`, fall back to `email`.
    """
    out: list[str] = []
    for it in items or []:
        if isinstance(it, str):
            s = it.strip()
            if s:
                out.append(s)
        elif isinstance(it, dict):
            name = (
                it.get("displayName")
                or it.get("name")
                or it.get("email")
                or ""
            ).strip()
            if name:
                out.append(name)
    return ", ".join(out)


def _ddmm(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%d/%m")


def _fmt_due(due_iso: str | None) -> str:
    """Render `due` as `DD.MM.YYYY` (operator-pinned format).
    Falls back to empty string on parse error."""
    if not due_iso:
        return ""
    try:
        d = datetime.fromisoformat(due_iso).date()
        return d.strftime("%d.%m.%Y")
    except ValueError:
        return ""


def _fmt_task_line(idx: int, t: dict[str, Any]) -> str:
    title = (t.get("title") or "").strip()
    desc = (t.get("description") or "").strip()
    if desc:
        # Keep desc compact — full body lives in Google Doc.
        if len(desc) > 220:
            desc = desc[:217].rstrip() + "…"
        head = f"{idx}) {title} - {desc}"
    else:
        head = f"{idx}) {title}"

    tail_parts: list[str] = []
    owner = (t.get("owner") or "").strip()
    if owner:
        tail_parts.append(owner)

    due = _fmt_due(t.get("due"))
    if due:
        tail_parts.append(due)

    status = (t.get("status") or "todo").strip().lower()
    status_label = _STATUS_LABEL.get(status, status)
    if status_label:
        tail_parts.append(f"[{status_label}]")

    if not tail_parts:
        return head
    return f"{head} — " + " • ".join(tail_parts)


def render_agenda_text(
    *,
    candidate: AgendaCandidate,
    output: AgendaOutput,
    doc_url: str | None,
) -> str:
    """Build the Slack-mrkdwn body in the operator-pinned style.

    The Doc URL is rendered as a plain hyperlink so Slack collapses
    it to «Подробно» — same look as Fireflies/Zoom summary cards.
    """
    lines: list[str] = []

    # Header — date + title + section name.
    lines.append(
        f"{_ddmm(candidate.scheduled_start_at)} - "
        f"{candidate.title} - Повестка"
    )

    names = _format_attendees(candidate.attendees)
    if names:
        lines.append("")
        lines.append(f"Участники: {names}")

    # «На прошлой встрече» — free prose joined from previous_recap
    # bullets. Operator wants a paragraph, not a bullet list.
    recap_blob = " ".join(
        item.strip().rstrip(".") + "." for item in output.previous_recap
        if item and item.strip()
    )
    if recap_blob:
        lines.append("")
        lines.append(f"На прошлой встрече: {recap_blob}")

    # «К обсуждению» = open tasks with status + free-form
    # discussion bullets the LLM produced. Tasks render as the
    # operator-pinned numbered list; open_questions append below
    # the task list as continuation items.
    discussion_items: list[dict[str, Any]] = list(output.tasks_checklist or [])
    # Treat free-form open_questions as no-status items so they
    # land in the same numbered list — operator pinned: «к
    # обсуждению: список задач из предыдущего и их статус».
    for q in output.open_questions or []:
        q = (q or "").strip()
        if not q:
            continue
        discussion_items.append({"title": q, "status": "todo"})

    if discussion_items:
        lines.append("")
        lines.append("К обсуждению:")
        lines.append("")
        for i, t in enumerate(discussion_items[:30], 1):
            lines.append(_fmt_task_line(i, t))

    if doc_url:
        lines.append("")
        lines.append(f"Подробно: {doc_url}")

    out = "\n".join(lines)
    # Slack's chat.postMessage `text` field is capped near 3000
    # chars; trim defensively so the post call doesn't fail.
    if len(out) > 2900:
        out = out[:2880].rstrip() + "\n…\nПодробно в Google Doc"
    return out


__all__ = ["render_agenda_text"]
