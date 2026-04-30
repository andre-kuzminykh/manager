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
You are a binary duplicate-detection classifier for newly
proposed tasks.

You receive ONE candidate task and up to 10 existing open tasks
(or pending drafts) from the same backlog. Decide whether the
candidate describes the SAME WORK as any one of them. Yes or no.

THE RULE — collapse when the candidate and an existing item
describe the same end-state. Two tasks are the same when they
overlap on:
  - the action (verb / verb-family — confirm, ask, send,
    organize a meeting, intro, follow up, prep, …), AND
  - the specific subject (named external entity, event,
    deliverable, project — ADNOC, Bosch deal, Ryan Gariepy
    meeting, the Q2 report, the Atuwatse Okorodudu intro).

The candidate's owner does NOT have to match — internal
team-owner attribution drifts between drafts. Due date does NOT
have to match — operator may set 18:00 today on one and tomorrow
on another for the same work.

Different EXTERNAL audience IS a discriminator: «отчёт Ирине»
≠ «отчёт Артёму» (two reports going to two different audiences).
But internal-team attribution between team members for ONE
external piece of work is NOT.

Default to FALSE when in doubt. Better one extra task the
operator merges than silently dropping real work. But when the
candidate clearly orbits the same external entity / event as
an existing item, return TRUE.

Output:
  is_duplicate: bool
  duplicate_of_task_id: integer task id of the matched item
                        (only when is_duplicate=true; null
                        otherwise)
  reason: one short sentence quoting the overlap.
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
    """Compact one-line-per-item listing fed to the LLM. Mark
    drafts with `D#` and tasks with `T#` so the model can address
    them separately when reporting which one is the duplicate."""
    if not items:
        return "(none)"
    lines: list[str] = []
    for it in items:
        prefix = "T#" if it.kind == "task" else "D#"
        desc = (it.description or "").replace("\n", " ").strip()
        if len(desc) > 200:
            desc = desc[:197] + "..."
        bits = [
            f"{prefix}{it.item_id}",
            it.title,
            f"owner={it.owner_label}",
            f"due={it.due_date}",
        ]
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


def _normalize_title_for_match(title: str) -> str:
    """FR-CR-05-97 — operator: «надо не расширять синонимы а
    поумнее их различать явно».

    Lowercase + collapse whitespace + strip Cyrillic/Latin
    punctuation noise. This isn't a synonym matcher — it's a
    «two LLM outputs landed nearly identical strings» backstop
    so the LLM dedup gate doesn't have to re-litigate exact
    matches under prompt-bloat noise.
    """
    if not title:
        return ""
    import re

    out = title.lower().strip()
    # Collapse internal whitespace.
    out = re.sub(r"\s+", " ", out)
    # Strip leading/trailing punctuation.
    out = out.strip(".!?,;:—-«»\"' ")
    # Replace Cyrillic ё → е (LLM often emits both for the same word).
    out = out.replace("ё", "е")
    return out


def _candidate_owner_uid(candidate: dict[str, Any]) -> str:
    return str(
        candidate.get("owner_user_id")
        or candidate.get("owner_display_name")
        or ""
    ).strip().lower()


def _existing_owner_key(item: _ExistingItem) -> str:
    return (item.owner_label or "").strip().lower()


def _deterministic_duplicate(
    candidate: dict[str, Any], existing: list[_ExistingItem]
) -> _ExistingItem | None:
    """FR-CR-05-97 / -99 — fast-path duplicate check that skips
    the LLM entirely.

    Match when BOTH:
      - `_normalize_title_for_match(title)` (case + whitespace
        + ё/е normalised) is equal
      - owner key (uid or display_name, lowercased) is equal

    Due date is NOT part of the match — operator may set
    different dates on two drafts for the same work (FR-CR-05-99
    regression: «Запланировать встречу с Atuwatse Okorodudu» on
    2026-04-30 vs 2026-05-04 — same work, drift in operator's
    date estimate).

    Returns the matched `_ExistingItem` or `None`. Used as a
    pre-LLM gate in `check_duplicate`. The LLM still runs for
    everything else (paraphrase / synonym dedup remains the
    LLM's domain via `_SYSTEM_PROMPT`).
    """
    cand_title = _normalize_title_for_match(candidate.get("title") or "")
    if not cand_title:
        return None
    cand_owner = _candidate_owner_uid(candidate)

    for item in existing:
        if _normalize_title_for_match(item.title) != cand_title:
            continue
        if _existing_owner_key(item) != cand_owner:
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

    # FR-CR-05-97 — deterministic exact-match fast path.
    fast = _deterministic_duplicate(candidate, existing)
    if fast is not None:
        log.info(
            "task_dedup_deterministic_hit",
            item_id=fast.item_id,
            kind=fast.kind,
        )
        return DedupResult(
            is_duplicate=True,
            duplicate_of_task_id=fast.item_id if fast.kind == "task" else None,
            reason=(
                "deterministic match: same title + owner + due_date "
                f"as {fast.kind} #{fast.item_id}"
            ),
        )

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
