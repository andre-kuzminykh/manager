"""FR-CR-05-168 — DB lookup for counterparty context.

`lookup_org(session, *, org_name)` returns a `CounterpartyContext`
with:
  * `counterparty_id` — id in `counterparties` if matched by
    normalised name;
  * `past_recordings` — list of zoom/fireflies rows whose title
    contains the normalised org name (newest first);
  * `open_tasks` — open Task rows linked to those past recordings
    via `source_conversation_id`;
  * `attributes` — JSON satellite from `counterparty_attributes`
    (FR-CR-05-124) if a hub row matched.

Pure-DB, no LLM. Used by both the runner and the one-shot CLI.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import (
    Counterparty,
    CounterpartyAttribute,
    MeetingRecording,
    Task,
    TaskStatus,
    ZoomRecording,
)

log = get_logger(__name__)


_PUNCT_STRIP = ".!?,;:—-«»\"'()[]{}/\\|"


def normalise_counterparty_name(s: str | None) -> str:
    if not s:
        return ""
    out = unicodedata.normalize("NFKD", s.lower().strip().replace("ё", "е"))
    out = "".join(ch for ch in out if not unicodedata.combining(ch))
    out = re.sub(r"[" + re.escape(_PUNCT_STRIP) + r"<>]", " ", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out


@dataclass
class CounterpartyContext:
    counterparty_id: int | None = None
    past_recordings: list[dict[str, Any]] = field(default_factory=list)
    open_tasks: list[dict[str, Any]] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)


def _recording_summary_row(r: ZoomRecording | MeetingRecording) -> dict[str, Any]:
    return {
        "id": getattr(r, "zoom_id", None) or getattr(r, "fireflies_id", None),
        "kind": "zoom" if isinstance(r, ZoomRecording) else "fireflies",
        "zoom_id": getattr(r, "zoom_id", None),
        "title": r.title or "",
        "meeting_date": r.meeting_date.isoformat() if r.meeting_date else None,
        "short_summary": r.short_summary or "",
        "google_doc_url": r.google_doc_url or "",
    }


def _open_task_row(t: Task) -> dict[str, Any]:
    return {
        "id": t.id,
        "title": t.title or "",
        "description": (t.description or "")[:300],
        "status": getattr(t.status, "value", str(t.status)),
        "priority": getattr(t.priority, "value", str(t.priority)),
        "owner_display_name": t.owner_display_name or "",
        "owner_user_id": t.owner_user_id or "",
        "due_date": t.due_date.isoformat() if t.due_date else None,
    }


def lookup_org(
    session: Session,
    *,
    org_name: str,
    lookback_days: int = 365,
    now: datetime | None = None,
) -> CounterpartyContext:
    """Resolve an org name to its hub row + recent context.

    Match strategy (case-/diacritic-insensitive):
      1. `counterparties.name_normalised`
      2. `zoom_recordings.title` / `meeting_recordings.title`
         contains the normalised key

    Newest recording first. Open tasks deduped by id.
    """
    key = normalise_counterparty_name(org_name)
    if not key:
        return CounterpartyContext()
    now = now or datetime.now(timezone.utc)

    counterparty_id: int | None = None
    attributes: dict[str, Any] = {}

    try:
        cp = (
            session.query(Counterparty)
            .filter(Counterparty.name_normalised == key)
            .first()
        )
        if cp is not None:
            counterparty_id = cp.id
            # Pull every satellite row, merge into one flat dict.
            attrs_rows = (
                session.query(CounterpartyAttribute)
                .filter(CounterpartyAttribute.counterparty_id == cp.id)
                .all()
            )
            for r in attrs_rows:
                payload = r.payload or {}
                if isinstance(payload, dict):
                    attributes.update(payload)
    except Exception as e:  # noqa: BLE001
        log.info("brief_lookup_counterparties_failed", error=str(e))

    past_recordings: list[dict[str, Any]] = []

    # Zoom rows
    try:
        zrows = list(
            session.execute(
                select(ZoomRecording)
                .where(ZoomRecording.meeting_date.is_not(None))
                .where(ZoomRecording.meeting_date < now)
                .order_by(ZoomRecording.meeting_date.desc())
            ).scalars().all()
        )
    except Exception as e:  # noqa: BLE001
        log.info("brief_lookup_zoom_failed", error=str(e))
        zrows = []
    # Fireflies rows
    try:
        frows = list(
            session.execute(
                select(MeetingRecording)
                .where(MeetingRecording.meeting_date.is_not(None))
                .where(MeetingRecording.meeting_date < now)
                .order_by(MeetingRecording.meeting_date.desc())
            ).scalars().all()
        )
    except Exception as e:  # noqa: BLE001
        log.info("brief_lookup_fireflies_failed", error=str(e))
        frows = []

    for r in (*zrows, *frows):
        if not r.title:
            continue
        if key in normalise_counterparty_name(r.title):
            past_recordings.append(_recording_summary_row(r))

    past_recordings.sort(
        key=lambda x: x.get("meeting_date") or "", reverse=True
    )
    past_recordings = past_recordings[:5]

    open_tasks: list[dict[str, Any]] = []
    zoom_ids = [
        r["zoom_id"] for r in past_recordings
        if r.get("kind") == "zoom" and r.get("zoom_id")
    ]
    if zoom_ids:
        try:
            trows = list(
                session.execute(
                    select(Task)
                    .where(Task.deleted_at.is_(None))
                    .where(Task.source_kind == "zoom")
                    .where(Task.source_conversation_id.in_(zoom_ids))
                    .where(Task.status != TaskStatus.done)
                ).scalars().all()
            )
        except Exception as e:  # noqa: BLE001
            log.info("brief_lookup_tasks_failed", error=str(e))
            trows = []
        priority_weight = {"urgent": 0, "high": 1, "medium": 2, "low": 3}

        def sort_key(t: Task) -> tuple[int, int]:
            pw = priority_weight.get(
                getattr(t.priority, "value", str(t.priority)), 9
            )
            return (pw, t.id)
        trows.sort(key=sort_key)
        open_tasks = [_open_task_row(t) for t in trows[:20]]

    return CounterpartyContext(
        counterparty_id=counterparty_id,
        past_recordings=past_recordings,
        open_tasks=open_tasks,
        attributes=attributes,
    )


__all__ = ["CounterpartyContext", "lookup_org", "normalise_counterparty_name"]
