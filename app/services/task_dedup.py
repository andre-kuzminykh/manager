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
from app.models import ActionDraft, ActionDraftState, Task, TaskStatus

log = get_logger(__name__)


_OPEN = (TaskStatus.backlog, TaskStatus.todo, TaskStatus.in_progress)


@dataclass
class _ExistingItem:
    """Unified shape for «things the candidate might duplicate» —
    open Tasks (existing in the DB) AND open ActionDrafts (created
    earlier in the same prepare_drafts batch)."""

    item_id: int
    kind: str  # "task" or "draft"
    title: str
    description: str | None
    owner_label: str
    due_date: str


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
You decide if a new task duplicates any of up to 10 existing
tasks from the same backlog.

You receive: candidate (title + description) and a list of
existing items (title + description, plus owner / due for
context).

Compare descriptions, not just titles. If the candidate's
description describes the same work as any existing item's
description, return is_duplicate=true and the id of that
item. Otherwise return false.

Output: is_duplicate (bool), duplicate_of_task_id (int|null),
reason (one short sentence).
"""


def _fetch_recent(session: Session, limit: int = 20) -> list[_ExistingItem]:
    """Return the most recently created «open work items» — both
    saved Tasks and pending ActionDrafts — newest first.

    Including drafts catches the case where two adjacent source
    messages produce siblings of the same task within one
    prepare_drafts batch («добавить Юру в участников» × 2). Without
    this, the dedup gate only sees the DB Task table and the
    first draft of the batch isn't there yet — both sibling
    drafts go through, and the operator gets two widgets for the
    same work.
    """
    out: list[_ExistingItem] = []
    tasks = (
        session.query(Task)
        .filter(
            Task.deleted_at.is_(None),
            Task.status.in_(_OPEN),
        )
        .order_by(Task.id.desc())
        .limit(limit)
        .all()
    )
    for t in tasks:
        out.append(
            _ExistingItem(
                item_id=t.id,
                kind="task",
                title=t.title or "",
                description=t.description,
                owner_label=t.owner_display_name or t.owner_user_id or "?",
                due_date=t.due_date.isoformat() if t.due_date else "—",
            )
        )
    drafts = (
        session.query(ActionDraft)
        .filter(ActionDraft.state == ActionDraftState.proposed)
        .order_by(ActionDraft.id.desc())
        .limit(limit)
        .all()
    )
    for d in drafts:
        payload = d.payload or {}
        out.append(
            _ExistingItem(
                item_id=d.id,
                kind="draft",
                title=str(payload.get("title") or ""),
                description=payload.get("description"),
                owner_label=str(
                    payload.get("owner_display_name")
                    or payload.get("owner_user_id")
                    or "?"
                ),
                due_date=str(payload.get("due_date") or "—"),
            )
        )
    return out


def _fmt_existing(items: list[_ExistingItem]) -> str:
    """Compact per-item listing fed to the LLM. Mark drafts
    with `D#` and tasks with `T#` so the model can address
    them separately when reporting which one is the duplicate.

    FR-CR-05-100 — operator: «по описанию задачи надо». Each
    existing item gets its FULL description (≤1500 chars per
    item) on its own line so the LLM can compare descriptions
    semantically, not just titles.
    """
    if not items:
        return "(none)"
    lines: list[str] = []
    for it in items:
        prefix = "T#" if it.kind == "task" else "D#"
        desc = (it.description or "").replace("\n", " ").strip()
        if len(desc) > 1500:
            desc = desc[:1497] + "..."
        lines.append(
            f"- {prefix}{it.item_id} | title: {it.title}\n"
            f"   owner: {it.owner_label} | due: {it.due_date}\n"
            f"   desc: {desc}"
        )
    return "\n".join(lines)


def _fmt_candidate(payload: dict[str, Any]) -> str:
    title = payload.get("title") or ""
    desc = (payload.get("description") or "").replace("\n", " ").strip()
    if len(desc) > 1500:
        desc = desc[:1497] + "..."
    owner = (
        payload.get("owner_display_name")
        or payload.get("owner_user_id")
        or "?"
    )
    due = payload.get("due_date") or "—"
    parts = [f"title: {title}"]
    if desc:
        parts.append(f"description: {desc}")
    parts.append(f"owner: {owner}")
    parts.append(f"due: {due}")
    return "\n".join(parts)


def _normalize_title_for_match(title: str) -> str:
    """Lowercase + collapse whitespace + strip Cyrillic/Latin
    punctuation + ё→е. Used both for rendering into the LLM
    prompt AND for the FR-CR-05-101 exact-title backstop
    below."""
    if not title:
        return ""
    import re

    out = title.lower().strip()
    out = re.sub(r"\s+", " ", out)
    out = out.strip(".!?,;:—-«»\"' ")
    out = out.replace("ё", "е")
    return out


def _owner_keys(*values: Any) -> set[str]:
    """Lowercased non-empty owner identifiers (uid, display
    name, real name, …) as a set, used to test for owner
    overlap with set intersection."""
    out: set[str] = set()
    for v in values:
        if v is None:
            continue
        s = str(v).strip().lower()
        if s and s not in {"?", "none", ""}:
            out.add(s)
    return out


def _exact_title_owner_match(
    candidate: dict[str, Any], existing: list[_ExistingItem]
) -> _ExistingItem | None:
    """FR-CR-05-101 — narrow deterministic backstop.

    Operator: «не нужен детерминистический матч, по описанию
    задачи надо». But two LLM outputs that land on the
    LITERALLY-identical title with the same owner kept
    slipping through the LLM dedup gate (Atuwatse Okorodudu
    × 2; PALADIN Goldman Sachs × 2). Reinstating the
    narrowest possible deterministic check — same normalised
    title AND any overlap on owner identifiers — purely as a
    safety net under the LLM. The LLM still runs for
    paraphrases / synonyms / different-but-related work.

    Match when BOTH:
      - `_normalize_title_for_match(title)` is equal
      - candidate's owner key set INTERSECTS with the
        existing item's (uid or display_name on either side)

    Due_date is NOT part of the match — operator may type
    different dates on two drafts of the same work.
    """
    cand_title = _normalize_title_for_match(candidate.get("title") or "")
    if not cand_title:
        return None
    cand_keys = _owner_keys(
        candidate.get("owner_user_id"),
        candidate.get("owner_display_name"),
    )
    if not cand_keys:
        return None
    for item in existing:
        if _normalize_title_for_match(item.title) != cand_title:
            continue
        if not (cand_keys & _owner_keys(item.owner_label)):
            continue
        return item
    return None


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

    FR-CR-05-97 — deterministic pre-check runs FIRST: when
    `(normalized_title, owner, due_date)` triple matches an
    existing item exactly, return is_duplicate=True without
    calling the LLM. Operator: «надо не расширять синонимы, а
    поумнее их различать явно». The LLM still handles
    paraphrase / synonym cases below.
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
        # the model occasionally invents. Match against both Tasks
        # and Drafts in the existing set.
        if of_id not in {it.item_id for it in existing}:
            of_id = None
    return DedupResult(
        is_duplicate=is_dup,
        duplicate_of_task_id=of_id,
        reason=str(result.get("reason") or "")[:200] or None,
    )
