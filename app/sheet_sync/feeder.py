"""DB → Sheet feeder: append NEW strategic tasks to the Sheet (append-only).

Sources (all 4 channels):
  • action_drafts  → Slack / Telegram (proposed). direction backfilled with
    gpt-4o-mini if missing (so it works even if ingest didn't classify yet).
  • tasks          → Fireflies / Zoom meetings (direction in task.extra).

A source row that (a) carries a strategic `direction` (DIRECTIONS_IMPORTANT)
and (b) has a title is appended as a Sheet row and recorded in
gs_exported_sources keyed by (source_kind, source_id) so it's never
re-appended. Existing rows / user edits are never touched — the Sheet→DB
engine handles those.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select

from app.logging_setup import get_logger
from app.models.intent import ActionDraft, ActionDraftState
from app.models.sheet_sync import GsExportedSource
from app.models.task import Task, TaskSourceKind
from app.services.task_direction import DIRECTIONS_IMPORTANT, classify_directions
from app.services.team_members import get_humans_for_matcher
from app.sheet_sync.config import PRIORITY_DISPLAY_BY_KEY, STATUS_DISPLAY_BY_KEY

log = get_logger(__name__)

_PRIORITY_DISPLAY = PRIORITY_DISPLAY_BY_KEY


def build_owner_resolver(session) -> tuple[Callable[[str], str], list[str]]:
    """Returns (resolve(raw)->valid_team_name|'', sorted_team_names)."""
    by_u: dict[str, str] = {}
    by_n: dict[str, str] = {}
    valid: set[str] = set()
    for h in get_humans_for_matcher(session):
        rn = (h.get("real_name") or "").strip()
        if not rn:
            continue
        valid.add(rn)
        u = (h.get("tg_username") or "").strip().lower()
        if u:
            by_u[u] = rn
        by_n.setdefault(rn.lower(), rn)
        first = rn.lower().split()[0] if rn.split() else ""
        if first:
            by_n.setdefault(first, rn)

    def resolve(raw: str) -> str:
        o = (raw or "").strip()
        if not o:
            return ""
        if o.startswith("@"):
            return by_u.get(o[1:].strip().lower(), "")
        base = o
        for sep in (" - ", " — ", " /", " ("):
            if sep in base:
                base = base.split(sep, 1)[0].strip()
        k = base.lower()
        name = by_n.get(k) or (by_n.get(k.split()[0]) if k.split() else None)
        return name if (name and name in valid) else ""

    return resolve, sorted(valid)


def _added_at(dt) -> str:
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%d %H:%M")
    return ""


def _row(*, title, description, owner, status, priority_key, direction, due, added_at):
    """Build a Sheet row (TASK_HEADERS order, incl. trailing Added at)."""
    return [
        title,
        description or "",
        owner,
        status,
        _PRIORITY_DISPLAY.get((priority_key or "medium").lower(), "Medium"),
        (direction or "").capitalize(),
        "", "",                      # start date/time
        due or "", "",               # deadline date/time
        "", "",                      # completion date/time
        "",                          # comments
        added_at,                    # Added at
    ]


def _backfill_draft_directions(session, drafts, *, llm, model) -> None:
    """Classify drafts missing a stored direction (gpt-4o-mini, chunk 5),
    persist to payload. No-op without an llm backend."""
    if llm is None:
        return
    todo = [d for d in drafts if not (d.payload or {}).get("direction")]
    if not todo:
        return
    chunk = 5
    for i in range(0, len(todo), chunk):
        batch = todo[i : i + chunk]
        mapping = classify_directions(
            tasks=[{"id": d.id, "title": (d.payload or {}).get("title") or "",
                    "description": (d.payload or {}).get("description") or ""} for d in batch],
            meeting_context=None, llm_backend=llm, model=model,
        )
        for d in batch:
            direction = mapping.get(d.id)
            if direction:
                p = dict(d.payload or {})
                p["direction"] = direction
                d.payload = p
    session.flush()


def feed_new_strategic(
    session, client, *, integration_id: str, since_dt, status: str = "To Do",
    llm=None, classify_model: str = "gpt-4o-mini",
) -> int:
    """Append strategic, titled, not-yet-exported tasks from ALL sources."""
    resolve, team_names = build_owner_resolver(session)
    exported = {
        (sk, sid) for sk, sid in session.execute(
            select(GsExportedSource.source_kind, GsExportedSource.source_id)
            .where(GsExportedSource.integration_id == integration_id)
        ).all()
    }

    rows: list[list[str]] = []
    new: list[tuple[str, str]] = []

    # --- A) action_drafts (Slack / Telegram) --------------------------------
    drafts = session.execute(
        select(ActionDraft)
        .where(ActionDraft.state == ActionDraftState.proposed)
        .where(ActionDraft.created_at >= since_dt)
        .order_by(ActionDraft.id.asc())
    ).scalars().all()
    _backfill_draft_directions(session, drafts, llm=llm, model=classify_model)
    for d in drafts:
        if ("action_draft", str(d.id)) in exported:
            continue
        p = d.payload or {}
        if (p.get("direction") or "").strip().lower() not in DIRECTIONS_IMPORTANT:
            continue
        if not (p.get("title") or "").strip():
            continue
        rows.append(_row(
            title=(p.get("title") or "").strip(),
            description=(p.get("description") or "").strip(),
            owner=resolve(p.get("owner_display_name")),
            status=status,
            priority_key=p.get("priority"),
            direction=(p.get("direction") or "").strip().lower(),
            due=(p.get("due_date") or "").strip(),
            added_at=_added_at(d.created_at),
        ))
        new.append(("action_draft", str(d.id)))

    # --- B) tasks (Fireflies / Zoom meetings) -------------------------------
    meeting_tasks = session.execute(
        select(Task)
        .where(Task.source_kind.in_([TaskSourceKind.fireflies, TaskSourceKind.zoom]))
        .where(Task.deleted_at.is_(None))
        .where(Task.created_at >= since_dt)
        .order_by(Task.id.asc())
    ).scalars().all()
    for t in meeting_tasks:
        if ("task", str(t.id)) in exported:
            continue
        direction = ((t.extra or {}).get("direction") or "").strip().lower()
        if direction not in DIRECTIONS_IMPORTANT:
            continue
        if not (t.title or "").strip():
            continue
        rows.append(_row(
            title=t.title.strip(),
            description=(t.description or "").strip(),
            owner=resolve(t.owner_display_name),
            status=STATUS_DISPLAY_BY_KEY.get(t.status.value if t.status else "todo", "To Do"),
            priority_key=t.priority.value if t.priority else "medium",
            direction=direction,
            due=t.due_date.isoformat() if t.due_date else "",
            added_at=_added_at(t.created_at),
        ))
        new.append(("task", str(t.id)))

    if not rows:
        return 0

    client.append_rows(rows)
    client.ensure_structure(responsible_options=team_names)
    for sk, sid in new:
        session.add(GsExportedSource(integration_id=integration_id, source_kind=sk, source_id=sid))
    session.flush()
    log.info("sheet_sync_fed_new", integration_id=integration_id, count=len(rows),
             drafts=sum(1 for k, _ in new if k == "action_draft"),
             meeting_tasks=sum(1 for k, _ in new if k == "task"))
    return len(rows)


__all__ = ["build_owner_resolver", "feed_new_strategic"]
