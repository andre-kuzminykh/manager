"""DB → Sheet feeder: append NEW strategic tasks to the Sheet (append-only).

A new proposed action_draft that (a) carries a strategic `direction`
(DIRECTIONS_IMPORTANT) and (b) has a title is appended as a Sheet row, and
recorded in gs_exported_sources so it's never re-appended. Existing rows /
user edits are never touched — the Sheet→DB direction (engine) handles those.

Pure helpers (`draft_to_row`, owner resolution) are DB-free / testable; the
feed orchestration needs a session + sheet client.
"""
from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select

from app.logging_setup import get_logger
from app.models.intent import ActionDraft, ActionDraftState
from app.models.sheet_sync import GsExportedSource
from app.services.task_direction import DIRECTIONS_IMPORTANT
from app.services.team_members import get_humans_for_matcher

log = get_logger(__name__)

_PRIORITY_DISPLAY = {"low": "Low", "medium": "Medium", "high": "High", "urgent": "High"}


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


def draft_to_row(payload: dict, *, owner: str, status: str = "To Do") -> list[str]:
    """Map a draft payload → a Sheet row (TASK_HEADERS order)."""
    direction = (payload.get("direction") or "").strip().lower()
    return [
        (payload.get("title") or "").strip(),
        (payload.get("description") or "").strip(),
        owner,
        status,
        _PRIORITY_DISPLAY.get((payload.get("priority") or "medium").lower(), "Medium"),
        direction.capitalize(),
        "", "",                                  # start date/time
        (payload.get("due_date") or "").strip(), "",  # deadline date/time
        "", "",                                  # completion date/time
        "",                                      # comments
    ]


def feed_new_strategic(
    session, client, *, integration_id: str, since_dt, status: str = "To Do",
) -> int:
    """Append strategic, titled, not-yet-exported drafts to the Sheet.
    Marks them in gs_exported_sources. Returns appended count."""
    resolve, team_names = build_owner_resolver(session)
    exported = set(session.execute(
        select(GsExportedSource.source_id).where(GsExportedSource.integration_id == integration_id)
    ).scalars().all())

    drafts = session.execute(
        select(ActionDraft)
        .where(ActionDraft.state == ActionDraftState.proposed)
        .where(ActionDraft.created_at >= since_dt)
        .order_by(ActionDraft.id.asc())
    ).scalars().all()

    rows: list[list[str]] = []
    new_ids: list[str] = []
    for d in drafts:
        if str(d.id) in exported:
            continue
        p = d.payload or {}
        if (p.get("direction") or "").strip().lower() not in DIRECTIONS_IMPORTANT:
            continue
        if not (p.get("title") or "").strip():
            continue
        rows.append(draft_to_row(p, owner=resolve(p.get("owner_display_name")), status=status))
        new_ids.append(str(d.id))

    if not rows:
        return 0

    client.append_rows(rows)
    client.ensure_structure(responsible_options=team_names)
    for sid in new_ids:
        session.add(GsExportedSource(
            integration_id=integration_id, source_kind="action_draft", source_id=sid
        ))
    session.flush()
    log.info("sheet_sync_fed_new", integration_id=integration_id, count=len(rows))
    return len(rows)


__all__ = ["build_owner_resolver", "draft_to_row", "feed_new_strategic"]
