"""LLM-driven duplicate detection for newly proposed tasks.

When a Telegram message is classified into one or more `TaskDraft`s,
we don't want to spam the user's DM with widgets for tasks that
already exist in the local DB — the same instruction often repeats
across days («не забудьте про отчёт», «отправь договор»). This
helper checks every fresh candidate against the last 20 tasks and
returns a verdict: is it a duplicate of an existing one?

The check uses the same LLM backend the classifier already uses —
one extra tool-call per candidate, which is cheap relative to the
full classify pipeline.

The dedup window is intentionally small (20 tasks). Larger windows
inflate the prompt and cost without much accuracy gain, and we'd
rather miss a duplicate of a 3-month-old task than over-suppress
legitimately new work.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import Task, TaskStatus

log = get_logger(__name__)


_OPEN = (TaskStatus.backlog, TaskStatus.todo, TaskStatus.in_progress)


@dataclass
class DedupResult:
    """Outcome of a single dedup check."""

    is_duplicate: bool
    duplicate_of_task_id: int | None = None
    reason: str | None = None


_DEDUP_TOOL_NAME = "record_dedup_decision"
_DEDUP_TOOL_DESCRIPTION = (
    "Record whether a new task duplicates an existing one."
)
_DEDUP_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_duplicate": {
            "type": "boolean",
            "description": (
                "True when the new task describes the same work as "
                "one of the existing tasks listed."
            ),
        },
        "duplicate_of_task_id": {
            "type": ["integer", "null"],
            "description": "Task id of the duplicate. Null when is_duplicate is false.",
        },
        "reason": {
            "type": "string",
            "description": "One short sentence explaining the verdict.",
        },
    },
    "required": ["is_duplicate"],
}


_SYSTEM_PROMPT = """\
You decide whether a new task DUPLICATES an existing one.

Two tasks duplicate when they describe the SAME piece of work —
same deliverable, same target, same goal. Different wording for
the same thing IS a duplicate ("подготовить отчёт" ≈ "сделать
отчёт"). One having more / less context IS NOT a difference.

They are NOT duplicates when:
- the deliverable differs (a deck vs a report, even on the same
  project),
- they target different people, projects, or due dates,
- one is generic ("подготовить отчёт") and the other names a
  different specific subject ("отчёт по продажам" vs "отчёт по
  клиентам").

Return ``is_duplicate=true`` when the new task duplicates any of
the existing tasks listed; set ``duplicate_of_task_id`` to that
existing task's id. Otherwise return false.
"""


def _fetch_recent(session: Session, limit: int = 20) -> list[Task]:
    """Return the most recently created open Tasks, newest first."""
    return (
        session.query(Task)
        .filter(
            Task.deleted_at.is_(None),
            Task.status.in_(_OPEN),
        )
        .order_by(Task.id.desc())
        .limit(limit)
        .all()
    )


def _fmt_existing(tasks: list[Task]) -> str:
    """Compact one-line-per-task listing fed to the LLM. Trim
    description to 200 chars so the prompt stays bounded."""
    if not tasks:
        return "(none)"
    lines: list[str] = []
    for t in tasks:
        owner = t.owner_display_name or t.owner_user_id or "?"
        due = t.due_date.isoformat() if t.due_date else "—"
        desc = (t.description or "").replace("\n", " ").strip()
        if len(desc) > 200:
            desc = desc[:197] + "..."
        bits = [f"#{t.id}", t.title, f"owner={owner}", f"due={due}"]
        if desc:
            bits.append(f"desc={desc}")
        lines.append("- " + " | ".join(bits))
    return "\n".join(lines)


def _fmt_candidate(payload: dict[str, Any]) -> str:
    title = payload.get("title") or ""
    desc = payload.get("description") or ""
    owner = (
        payload.get("owner_display_name")
        or payload.get("owner_user_id")
        or "?"
    )
    due = payload.get("due_date") or "—"
    parts = [f"title: {title}"]
    if desc:
        parts.append(f"description: {desc[:500]}")
    parts.append(f"owner: {owner}")
    parts.append(f"due: {due}")
    return "\n".join(parts)


def check_duplicate(
    session: Session,
    *,
    candidate: dict[str, Any],
    llm_backend: Any | None,
    lookback: int = 10,
) -> DedupResult:
    """Return a :class:`DedupResult` for ``candidate``.

    ``candidate`` is a dict with at least `title`; `description`,
    `owner_*`, and `due_date` are optional. When no LLM backend is
    available or the lookback window is empty, returns
    «not a duplicate» — better to create than to silently drop.
    """
    if not candidate.get("title"):
        return DedupResult(is_duplicate=False)

    existing = _fetch_recent(session, limit=lookback)
    if not existing:
        return DedupResult(is_duplicate=False)

    if llm_backend is None or not hasattr(llm_backend, "call_tool"):
        return DedupResult(is_duplicate=False)

    user_prompt = (
        "EXISTING TASKS (most recent open):\n"
        f"{_fmt_existing(existing)}\n\n"
        "NEW CANDIDATE:\n"
        f"{_fmt_candidate(candidate)}"
    )
    try:
        result = llm_backend.call_tool(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            tool_name=_DEDUP_TOOL_NAME,
            tool_description=_DEDUP_TOOL_DESCRIPTION,
            tool_parameters=_DEDUP_TOOL_PARAMETERS,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("task_dedup_llm_failed", error=str(e))
        return DedupResult(is_duplicate=False)

    if not isinstance(result, dict):
        return DedupResult(is_duplicate=False)

    is_dup = bool(result.get("is_duplicate"))
    of_id = result.get("duplicate_of_task_id")
    if of_id is not None:
        try:
            of_id = int(of_id)
        except (TypeError, ValueError):
            of_id = None
        # Don't trust an id that isn't actually in our lookback set —
        # the model occasionally invents.
        if of_id not in {t.id for t in existing}:
            of_id = None
    return DedupResult(
        is_duplicate=is_dup,
        duplicate_of_task_id=of_id,
        reason=str(result.get("reason") or "")[:200] or None,
    )
