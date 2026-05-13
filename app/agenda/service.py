"""FR-CR-05-165 — Pre-meeting agenda service.

Pure-logic surface (no Calendar API / Slack / OpenAI calls here —
those live in the runner). Methods are deterministic and unit-
testable with an in-memory session.

Surface:
  * ``normalise_title(s)`` — lowercase + collapse ws + drop punct,
    same family as ``task_dedup._normalize_title_for_match`` but
    private (we don't want a cross-module breakage if dedup
    tweaks its rule).
  * ``find_prior_recordings(session, title, lookback_days)`` —
    list ``ZoomRecording`` rows where ``normalise_title(title) ==
    normalise_title(event.title)``. Used to decide
    «recurring-enough» and to feed the LLM compose step.
  * ``open_tasks_for_recordings(session, zoom_ids)`` — open tasks
    (status != done/cancelled) bound to any of the recordings'
    zoom_ids. Result is ordered: priority desc, due asc.
  * ``AgendaService.is_already_posted(session, event_id)`` and
    ``AgendaService.record_post(...)`` — idempotency helpers.

The LLM compose call + Slack/Doc posting live in
``app.agenda.runner`` because they need API clients (kept off
the unit-test path).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import (
    MeetingAgenda,
    Task,
    TaskStatus,
    ZoomRecording,
)

log = get_logger(__name__)


_PUNCT_STRIP = ".!?,;:—-«»\"'()[]{}/\\|"


def normalise_title(title: str | None) -> str:
    """Comparable key for meeting titles.

    Lowercase + ё→е + NFKD-fold + drop combining marks + strip
    punctuation + collapse whitespace. Matches the spirit of
    ``task_dedup._normalize_title_for_match`` but lives here so
    the agenda module stays self-contained.

    Examples:
      «Genia Xasis <> Humanoid (Weekly fundraising sync)»
        → «genia xasis humanoid weekly fundraising sync»
      «Лётучка - Подземелья»
        → «летучка подземелья»
    """
    if not title:
        return ""
    s = title.lower().strip()
    # ё→е BEFORE NFKD so we don't end up with a stray combining
    # diaeresis (NFKD decomposes ё into «е» + combining-mark; we
    # then strip the mark anyway, but doing the replace first
    # keeps the code easier to reason about).
    s = s.replace("ё", "е")
    s = unicodedata.normalize("NFKD", s)
    # Drop combining marks (category Mn) — handles every diacritic
    # the NFKD pass left behind, not just the Russian ё.
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[" + re.escape(_PUNCT_STRIP) + r"<>]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


@dataclass
class AgendaCandidate:
    """The result of a discovery tick: a calendar event we want
    to post an agenda for, plus everything we'll feed the LLM."""

    calendar_event_id: str
    recurring_event_id: str | None
    title: str
    title_normalised: str
    scheduled_start_at: datetime
    description: str | None = None
    attendees: list[str] = field(default_factory=list)
    prior_recordings: list[dict[str, Any]] = field(default_factory=list)
    open_tasks: list[dict[str, Any]] = field(default_factory=list)


def find_prior_recordings(
    session: Session,
    *,
    title: str,
    lookback_days: int,
    now: datetime | None = None,
) -> list[ZoomRecording]:
    """Return ZoomRecording rows whose title normalises to the
    same key, scheduled in the last ``lookback_days`` days.

    Newest first. Filters out the «in-progress / future-scheduled»
    rows by requiring ``meeting_date < now`` — we want the LAST
    instance, not the upcoming one.
    """
    key = normalise_title(title)
    if not key:
        return []
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max(1, int(lookback_days)))
    stmt = (
        select(ZoomRecording)
        .where(ZoomRecording.meeting_date.is_not(None))
        .where(ZoomRecording.meeting_date >= cutoff)
        .where(ZoomRecording.meeting_date < now)
        .order_by(ZoomRecording.meeting_date.desc())
    )
    rows = session.execute(stmt).scalars().all()
    out: list[ZoomRecording] = []
    for r in rows:
        if normalise_title(r.title) == key:
            out.append(r)
    return out


def open_tasks_for_recordings(
    session: Session,
    *,
    zoom_ids: list[str],
    limit: int = 30,
) -> list[Task]:
    """List Tasks whose ``source_kind='zoom'`` AND
    ``source_conversation_id ∈ zoom_ids`` AND status ∉ {done,
    cancelled} AND not soft-deleted.

    Ordered by: priority desc (urgent → low), due_date asc nulls
    last, created_at asc. Limited to keep prompt size sane.
    """
    if not zoom_ids:
        return []
    # Priority weights for ORDER BY — Python-side after SELECT.
    stmt = (
        select(Task)
        .where(Task.deleted_at.is_(None))
        .where(Task.source_kind == "zoom")
        .where(Task.source_conversation_id.in_(zoom_ids))
        .where(Task.status != TaskStatus.done)
    )
    rows = list(session.execute(stmt).scalars().all())

    priority_weight = {"urgent": 0, "high": 1, "medium": 2, "low": 3}

    def sort_key(t: Task) -> tuple[int, datetime, int]:
        pw = priority_weight.get(
            getattr(t.priority, "value", str(t.priority)), 9
        )
        due = t.due_date or datetime.max.date()
        return (pw, due, t.id)

    rows.sort(key=sort_key)
    return rows[: max(1, int(limit))]


def render_recording_for_prompt(r: ZoomRecording) -> dict[str, Any]:
    """Pick exactly the fields the agenda LLM needs — keeps the
    prompt small."""
    return {
        "zoom_id": r.zoom_id,
        "title": r.title or "",
        "meeting_date": r.meeting_date.isoformat() if r.meeting_date else None,
        "short_summary": r.short_summary or "",
        "detailed_summary": (r.detailed_summary or "")[:3500],
        "google_doc_url": r.google_doc_url or "",
        "tasks_extracted_count": r.tasks_extracted_count or 0,
    }


def render_task_for_prompt(t: Task) -> dict[str, Any]:
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


class AgendaService:
    """Idempotency + persistence facade. Stateless, just wraps
    the ORM so the runner stays readable."""

    def is_already_posted(
        self, session: Session, *, calendar_event_id: str
    ) -> bool:
        if not calendar_event_id:
            return False
        existing = (
            session.query(MeetingAgenda)
            .filter(
                MeetingAgenda.calendar_event_id == calendar_event_id
            )
            .first()
        )
        return existing is not None

    def record_post(
        self,
        session: Session,
        *,
        candidate: AgendaCandidate,
        slack_channel: str,
        slack_ts: str | None,
        google_doc_id: str | None,
        google_doc_url: str | None,
        prior_zoom_ids: list[str],
    ) -> MeetingAgenda:
        """Insert the idempotency row. Caller commits."""
        row = MeetingAgenda(
            calendar_event_id=candidate.calendar_event_id,
            recurring_event_id=candidate.recurring_event_id,
            title=candidate.title,
            title_normalised=candidate.title_normalised,
            scheduled_start_at=candidate.scheduled_start_at,
            posted_at=datetime.now(timezone.utc),
            slack_channel=slack_channel,
            slack_ts=slack_ts,
            google_doc_id=google_doc_id,
            google_doc_url=google_doc_url,
            prior_meeting_zoom_ids=prior_zoom_ids,
        )
        session.add(row)
        session.flush()
        return row


def build_candidates(
    session: Session,
    *,
    events: list[dict[str, Any]],
    lookback_days: int,
    min_prior_meetings: int,
    now: datetime | None = None,
) -> list[AgendaCandidate]:
    """Filter+enrich Calendar events into candidates ready for
    LLM compose.

    Drops events that:
      - have no usable title;
      - have fewer than ``min_prior_meetings`` prior recordings
        (= not «recurring enough»);
      - already have a `MeetingAgenda` row (== posted earlier).

    The returned list is sorted by ``scheduled_start_at`` ascending
    so the runner can post them in start-time order.
    """
    svc = AgendaService()
    out: list[AgendaCandidate] = []
    for ev in events or []:
        ev_id = (ev.get("id") or "").strip()
        title = (ev.get("title") or "").strip()
        start = ev.get("start")
        if not ev_id or not title or start is None:
            continue
        if isinstance(start, str):
            try:
                start = datetime.fromisoformat(start.replace("Z", "+00:00"))
            except ValueError:
                continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        # Skip already-posted.
        if svc.is_already_posted(session, calendar_event_id=ev_id):
            continue
        prior = find_prior_recordings(
            session, title=title, lookback_days=lookback_days, now=now,
        )
        if len(prior) < max(1, int(min_prior_meetings)):
            continue
        zoom_ids = [r.zoom_id for r in prior]
        open_tasks = open_tasks_for_recordings(
            session, zoom_ids=zoom_ids,
        )
        candidate = AgendaCandidate(
            calendar_event_id=ev_id,
            recurring_event_id=ev.get("recurring_event_id"),
            title=title,
            title_normalised=normalise_title(title),
            scheduled_start_at=start,
            description=ev.get("description"),
            attendees=list(ev.get("attendees") or []),
            prior_recordings=[
                render_recording_for_prompt(r) for r in prior
            ],
            open_tasks=[render_task_for_prompt(t) for t in open_tasks],
        )
        out.append(candidate)
    out.sort(key=lambda c: c.scheduled_start_at)
    return out


__all__ = [
    "AgendaCandidate",
    "AgendaService",
    "build_candidates",
    "find_prior_recordings",
    "normalise_title",
    "open_tasks_for_recordings",
    "render_recording_for_prompt",
    "render_task_for_prompt",
]
