"""FR-CR-05-165 — Slack mrkdwn renderer for the agenda DM.

Operator-pinned: «отправлять в слак коротко и гиперссылкой более
подробный контекст, формат `12/05 - Повестка ко встрече "__"`».

So the Slack message is:
  *12/05 — Повестка ко встрече «<title>»*

  📋 *Из прошлого раза*
  • …
  • …

  ✅ *Задачи и их статусы*
  ☐ Title (owner, до 15/05)
  ☑ Title — done (owner)
  …

  🎯 *К обсуждению*
  • …

  📄 <https://docs.google.com/...|Подробно (Google Doc)>

Keep the message under Slack's 3000-char text cap by truncating
each section if it gets long. The Doc link always carries the
full version.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.agenda.compose import AgendaOutput
from app.agenda.service import AgendaCandidate


_STATUS_BOX = {
    "todo": "☐",
    "in_progress": "▣",
    "blocked": "⛔",
    "done": "☑",
    "cancelled": "✕",
}


def _ddmm(dt: datetime) -> str:
    # Render the meeting date as DD/MM regardless of timezone; the
    # operator's source format on cards.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%d/%m")


def _fmt_due(due_iso: str | None) -> str:
    if not due_iso:
        return ""
    try:
        d = datetime.fromisoformat(due_iso).date()
        return f"до {d.strftime('%d/%m')}"
    except ValueError:
        return ""


def render_agenda_text(
    *,
    candidate: AgendaCandidate,
    output: AgendaOutput,
    doc_url: str | None,
) -> str:
    """Build the Slack-mrkdwn body. Doc URL is rendered as a
    hyperlink so the user sees a compact «Подробно» label.

    Long sections get truncated with «…» to stay under the
    3000-char message cap; the Doc carries the full version.
    """
    lines: list[str] = []
    header = (
        f"*{_ddmm(candidate.scheduled_start_at)} — Повестка ко встрече "
        f"«{candidate.title}»*"
    )
    lines.append(header)

    if output.previous_recap:
        lines.append("")
        lines.append("📋 *Из прошлого раза*")
        for item in output.previous_recap[:5]:
            line = (item or "").strip()
            if not line:
                continue
            lines.append(f"• {line}")

    if output.tasks_checklist:
        lines.append("")
        lines.append("✅ *Задачи и их статусы*")
        for t in output.tasks_checklist[:30]:
            status = (t.get("status") or "todo").strip().lower()
            box = _STATUS_BOX.get(status, "☐")
            title = (t.get("title") or "").strip()
            owner = (t.get("owner") or "").strip()
            due = _fmt_due(t.get("due"))
            tail_parts = [p for p in (owner, due) if p]
            tail = f" ({', '.join(tail_parts)})" if tail_parts else ""
            lines.append(f"{box} {title}{tail}")

    if output.open_questions:
        lines.append("")
        lines.append("🎯 *К обсуждению*")
        for item in output.open_questions[:4]:
            line = (item or "").strip()
            if not line:
                continue
            lines.append(f"• {line}")

    if doc_url:
        lines.append("")
        lines.append(f"📄 <{doc_url}|Подробно (Google Doc)>")

    out = "\n".join(lines)
    # Slack's chat.postMessage cap is 40 KB for blocks but ~3000 chars
    # for the legacy `text` field. We post via `text` for simplicity,
    # so trim defensively.
    if len(out) > 2900:
        out = out[:2880] + "\n…\n📄 detail in Google Doc"
    return out


__all__ = ["render_agenda_text"]
