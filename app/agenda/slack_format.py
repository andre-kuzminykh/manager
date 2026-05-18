"""FR-CR-05-165 — Slack mrkdwn renderer for the agenda DM.

FR-CR-05-167 operator-pinned 2026-05-14 — final format:

    *<doc-url|14/05 - Агенда к Fundraising daily>*

    Участники: A, B, C

    На прошлой встрече: <free prose, single label>

    Статус задач к обсуждению:

    1) <task title> - <description> — <owner> • DD.MM.YYYY HH:MM [<status>]
    2) ...

The header line is a Slack-style hyperlink to the Google Doc when
one was created — clicking opens the full agenda (full task list,
no Slack 3000-char cap). Without a doc URL we render plain bold
text.

Status suffix appears only when the task is NOT `todo` — keeps
default-state items visually clean.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.agenda.compose import AgendaOutput
from app.agenda.service import AgendaCandidate


_STATUS_LABEL = {
    # FR-CR-05-167 2026-05-14 — operator's reference format does
    # not show a `[status]` suffix; we suppress it for all states
    # by default. Closed states (done / cancelled) still get a
    # label — operator wants to see that a previous-meeting task
    # is already finished.
    "todo": "",
    "in_progress": "",
    "blocked": "",
    "done": "done",
    "cancelled": "cancelled",
}

# Slack `text` field is capped near 3000 chars; we cap a bit
# tighter so the trailing «… полный список в Google Doc» line
# fits.
_SLACK_TEXT_CAP = 2900
# Cap the number of tasks we render directly in Slack — the full
# list always lives in the Doc.
_SLACK_TASK_LIMIT = 12


def _slack_safe(text: str) -> str:
    """Escape Slack mrkdwn metachars `<` `>` so user-supplied
    text (meeting titles, task names, descriptions) doesn't get
    mis-parsed as link / mention markup. Same rule as
    `_slack_link_label_safe` but without the pipe → slash swap
    (which is only needed inside `<url|label>` blocks)."""
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
    )


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


def _slack_link_label_safe(text: str) -> str:
    """Slack mrkdwn link is ``<URL|LABEL>``; literal `<`, `>`, `|`
    inside LABEL break the parser and the link renders as raw
    text. Operator hit this with «EQT Group <> Humanoid / Intro
    call» — the `<>` in the title turned the entire agenda
    message into garbage on the client side.

    Replace with the visually similar Unicode small angles
    (U+2039 / U+203A) and pipe → slash, matching the
    `slack_mirror._link_sub` rule used elsewhere in the project.
    """
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


def _fmt_due(due_iso: str | None, due_time: str | None = None) -> str:
    """Render `due` as `DD.MM.YYYY` or `DD.MM.YYYY HH:MM` when a
    time is available. Falls back to empty string on parse
    error."""
    if not due_iso:
        return ""
    try:
        d = datetime.fromisoformat(due_iso).date()
    except ValueError:
        return ""
    base = d.strftime("%d.%m.%Y")
    if due_time and isinstance(due_time, str) and due_time.strip():
        return f"{base} {due_time.strip()}"
    return base


def _strip_recap_label(text: str) -> str:
    """LLM occasionally re-prepends «На прошлой встрече: » even
    though the prompt says not to. Strip it so the renderer's own
    label isn't doubled up.

    Operator-pinned: «не надо два раза писать "на прошлой встрече"».
    """
    if not text:
        return ""
    s = text.strip()
    lower = s.lower()
    for prefix in ("на прошлой встрече:", "на прошлой встрече ", "на прошлой встрече —", "на прошлой встрече,"):
        if lower.startswith(prefix):
            s = s[len(prefix):].lstrip(" :,-—")
            break
    return s


def _fmt_task_line(idx: int, t: dict[str, Any]) -> str:
    title = _slack_safe((t.get("title") or "").strip())
    desc = _slack_safe((t.get("description") or "").strip())
    if desc:
        if len(desc) > 220:
            desc = desc[:217].rstrip() + "…"
        head = f"{idx}) {title} - {desc}"
    else:
        head = f"{idx}) {title}"

    tail_parts: list[str] = []
    owner = _slack_safe((t.get("owner") or "").strip())
    if owner:
        tail_parts.append(owner)

    due = _fmt_due(t.get("due"), t.get("due_time"))
    if due:
        tail_parts.append(due)

    status = (t.get("status") or "todo").strip().lower()
    status_label = _STATUS_LABEL.get(status, status)
    if status_label:
        tail_parts.append(f"[{status_label}]")

    if not tail_parts:
        return head
    return f"{head} — " + " • ".join(tail_parts)


def _render_header(candidate: AgendaCandidate, doc_url: str | None) -> str:
    """`<url|DD/MM - Агенда к <title>>` — Slack mrkdwn hyperlink.
    Without a URL: plain bold text. Same shape Fireflies / Zoom
    use for the meeting summary header line.

    FR-CR-05-167 bugfix 2026-05-14: titles like «EQT Group <>
    Humanoid» have literal `<>` characters which Slack would
    otherwise interpret as more link markup, breaking the entire
    message. Escape them to Unicode small angles before wrapping
    in the link.
    """
    safe_title = _slack_link_label_safe(candidate.title)
    label = (
        f"{_ddmm(candidate.scheduled_start_at)} - Агенда к {safe_title}"
    )
    if doc_url:
        return f"*<{doc_url}|{label}>*"
    return f"*{label}*"


def _collect_discussion_items(output: AgendaOutput) -> list[dict[str, Any]]:
    discussion_items: list[dict[str, Any]] = list(output.tasks_checklist or [])
    for q in output.open_questions or []:
        q = (q or "").strip()
        if q:
            discussion_items.append({"title": q, "status": "todo"})
    return discussion_items


def render_agenda_text(
    *,
    candidate: AgendaCandidate,
    output: AgendaOutput,
    doc_url: str | None,
) -> str:
    """Top-of-thread agenda message: header + participants + recap.

    Operator-pinned 2026-05-18: «"…ещё N пунктов в Google Doc"
    так не пиши к агендам, лучше их в треды пиши все задачи, а
    суть пиши в сообщении». Task list moves to thread replies —
    see ``render_agenda_task_thread_replies``.
    """
    lines: list[str] = []
    lines.append(_render_header(candidate, doc_url))

    names = _slack_safe(_format_attendees(candidate.attendees))
    if names:
        lines.append("")
        lines.append(f"Участники: {names}")

    # Recap rendered as a single paragraph under
    # «На прошлой встрече: » — operator-pinned 2026-05-14 (2nd
    # revision): summaries use «Суть:» / «To-Do:»; agendas use
    # «На прошлой встрече:» / «Статус задач к обсуждению:».
    recap_blob = " ".join(
        item.strip().rstrip(".") + "." for item in output.previous_recap
        if item and item.strip()
    )
    recap_blob = _slack_safe(_strip_recap_label(recap_blob).strip())
    if recap_blob:
        lines.append("")
        lines.append(f"На прошлой встрече: {recap_blob}")

    items = _collect_discussion_items(output)
    if items:
        lines.append("")
        word = "пункт" if len(items) == 1 else (
            "пункта" if 2 <= len(items) % 10 <= 4
            and not (12 <= len(items) % 100 <= 14)
            else "пунктов"
        )
        lines.append(
            f"👇 {len(items)} {word} к обсуждению — в треде ниже"
        )

    out = "\n".join(lines)
    if len(out) > _SLACK_TEXT_CAP:
        suffix = "\n…\nполный список в Google Doc" if doc_url else "\n…"
        cap = _SLACK_TEXT_CAP - len(suffix)
        out = out[:cap].rstrip() + suffix
    return out


def render_agenda_task_thread_replies(
    *,
    output: AgendaOutput,
) -> list[str]:
    """Build one or more Slack-mrkdwn thread replies containing
    the full numbered task list. Splits into chunks when a single
    body would exceed ``_SLACK_TEXT_CAP`` — no truncation, no
    «…ещё N в Google Doc» overflow.

    Returns ``[]`` when there are no items to render.
    """
    items = _collect_discussion_items(output)
    if not items:
        return []

    header = "Статус задач к обсуждению:"
    chunks: list[str] = []
    current: list[str] = [header, ""]
    current_len = len(header) + 1
    for i, t in enumerate(items, 1):
        line = _fmt_task_line(i, t)
        # +1 for the newline that joins this line
        if current_len + len(line) + 1 > _SLACK_TEXT_CAP and current:
            chunks.append("\n".join(current).rstrip())
            current = [header + f" (продолжение, {i}–)", ""]
            current_len = len(current[0]) + 1
        current.append(line)
        current_len += len(line) + 1
    if current:
        chunks.append("\n".join(current).rstrip())
    return chunks


__all__ = [
    "render_agenda_task_thread_replies",
    "render_agenda_text",
]
