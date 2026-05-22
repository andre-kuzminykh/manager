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
    Employee,
    MeetingAgenda,
    Task,
    TaskStatus,
    TeamMember,
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


# FR-CR-05-167 — operator-pinned fallback. The Google Sheet
# `team_members.email` is wipe-and-replace synced, and the
# operator doesn't always keep emails in the Sheet — so a sync
# can leave the DB without any email→name mapping. To stop the
# agenda DM from regressing to raw emails, we keep a hard-coded
# core dictionary here as the absolute last fallback.
# Update only with operator confirmation; otherwise edit the
# Sheet (which still wins — DB rows override this map).
_AGENDA_EMAIL_NAME_FALLBACK: dict[str, str] = {
    "1@thehumanoid.ai":               "Артем Соколов",
    "kaa@thehumanoid.ai":             "Alina Kolpakova",
    "oponomarenko@cohengresser.com":  "Ольга Пономаренко",
    "dmitry.sedov@thehumanoid.ai":    "Дмитрий Седов",
    "dmitry.sedov@sedovbrothers.com": "Дмитрий Седов",
    "irina.shipilova@thehumanoid.ai": "Ирина Шипилова",
    "irina.shipilova@skl.vc":         "Ирина Шипилова",
    "elena.radionova@sokolov.ch":     "Елена Радионова",
}


def _build_email_to_name_map(session: Session) -> dict[str, str]:
    """FR-CR-05-167 — operator-pinned 2026-05-14: «переводи почты
    в конкретные имена из списка людей (у нас есть в бд)».

    Resolution chain (last write wins):

      0. Hard-coded `_AGENDA_EMAIL_NAME_FALLBACK` — always
         present so wipe-and-replace sync of `team_members`
         doesn't regress the agenda DM to raw emails.
      1. `employees` (Slack ingest sync from `users.info`).
         Empty when Slack scope `users:read.email` isn't
         granted.
      2. `team_members` (hand-curated Sheet; FR-CR-05-10). Highest
         priority — operator-curated row WINS over both Slack
         and the hard-coded fallback.
    """
    out: dict[str, str] = dict(_AGENDA_EMAIL_NAME_FALLBACK)
    try:
        rows = (
            session.query(Employee)
            .filter(Employee.email.is_not(None))
            .all()
        )
        for e in rows:
            email = (e.email or "").strip().lower()
            name = (e.real_name or e.display_name or "").strip()
            if email and name:
                out[email] = name
    except Exception as e:  # noqa: BLE001
        log.info("agenda_email_resolve_employees_query_failed", error=str(e))
    try:
        rows = (
            session.query(TeamMember)
            .filter(TeamMember.email.is_not(None))
            .filter(TeamMember.active.is_(True))
            .all()
        )
        for tm in rows:
            email = (tm.email or "").strip().lower()
            name = (tm.real_name or "").strip()
            if email and name:
                # team_members wins — it's the hand-curated Sheet.
                out[email] = name
    except Exception as e:  # noqa: BLE001
        log.info("agenda_email_resolve_team_members_query_failed", error=str(e))
    return out


def _resolve_attendee_label(item: Any, email_to_name: dict[str, str]) -> str:
    """Best-effort: turn a Calendar API attendee item into a
    display label. Falls back to displayName → email when no
    DB hit; drops garbage."""
    if isinstance(item, str):
        s = item.strip()
        # If the string is itself an email, try the lookup.
        if s and "@" in s:
            cand = email_to_name.get(s.lower())
            if cand:
                return cand
        return s
    if not isinstance(item, dict):
        return ""
    email = (item.get("email") or "").strip().lower()
    if email:
        cand = email_to_name.get(email)
        if cand:
            return cand
    return (
        (item.get("displayName") or item.get("name") or item.get("email") or "")
        .strip()
    )


def _resolve_attendees(
    items: list[Any], session: Session
) -> list[str]:
    """Map a list of Calendar attendee dicts to display names via
    `team_members` / `employees` email lookup. Unresolved entries
    fall back to displayName → email — never raises, returns an
    ordered, dedup-by-label list."""
    if not items:
        return []
    email_to_name = _build_email_to_name_map(session)
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        label = _resolve_attendee_label(it, email_to_name)
        if not label:
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(label)
    return out


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
    # Resolved display labels (real names or email fallback) — see
    # `_resolve_attendees`. Calendar API gives dict shapes; we
    # flatten them to strings before storing on the candidate.
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
    limit: int = 100,
) -> list[Task]:
    """List Tasks whose ``source_kind='zoom'`` AND
    ``source_conversation_id ∈ zoom_ids`` AND status ∉ {done,
    cancelled} AND not soft-deleted.

    FR-CR-05-192aa — фильтр по `extra.direction ∈ DIRECTIONS_IMPORTANT`.
    Все задачи остаются в БД (storage = unfiltered), но в тред агенды
    попадают только важные направления (investors / deliverables /
    budget / design / beta). Тasks с `direction = 'other'` или БЕЗ
    direction (unclassified) — НЕ попадают. Operator escape-hatch:
    `AGENDA_TASK_FILTER_DIRECTIONS_DISABLED=true` отключает фильтр.

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

    # FR-CR-05-192aa direction filter (in-Python to avoid jsonb-cast
    # SQLAlchemy dialect headaches on json column type)
    import os
    filter_disabled = os.environ.get(
        "AGENDA_TASK_FILTER_DIRECTIONS_DISABLED", ""
    ).strip().lower() in ("true", "1", "yes", "on")
    if not filter_disabled:
        from app.services.task_direction import DIRECTIONS_IMPORTANT
        rows = [
            t for t in rows
            if isinstance(t.extra, dict)
            and t.extra.get("direction") in DIRECTIONS_IMPORTANT
        ]

    priority_weight = {"urgent": 0, "high": 1, "medium": 2, "low": 3}

    def sort_key(t: Task) -> tuple[int, datetime, int]:
        pw = priority_weight.get(
            getattr(t.priority, "value", str(t.priority)), 9
        )
        due = t.due_date or datetime.max.date()
        return (pw, due, t.id)

    rows.sort(key=sort_key)
    return rows[: max(1, int(limit))]


def _attendee_match_keys(label: str) -> set[str]:
    """Comparable keys for matching an attendee label against
    task owner fields. Lowercase + collapse whitespace + ё→е +
    drop diacritics so «Артём Соколов», «Артем  соколов», and
    «artem sokolov» all hash to the same set."""
    s = (label or "").strip().lower().replace("ё", "е")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return set()
    keys: set[str] = {s}
    # «Artem Sokolov» → also match «Artem» / «Sokolov» so a task
    # whose owner column carries only the first or last name still
    # matches an attendee with full name.
    for piece in s.split(" "):
        if len(piece) >= 3:
            keys.add(piece)
    return keys


def _filter_tasks_by_attendees(
    tasks: list[dict[str, Any]],
    attendees: list[str],
) -> list[dict[str, Any]]:
    """FR-CR-05-167 operator-pinned 2026-05-14: «возьми список
    участников и в агенде сделай что обсуждали / и что обсудить
    задачи только по участникам встречи».

    Drop tasks whose owner_display_name / owner_user_id doesn't
    overlap with the meeting attendees. Pass-through unchanged
    when:
      - attendees list is empty (no signal — show everything);
      - a task has no owner at all (don't hide unassigned).
    """
    if not attendees:
        return tasks
    attendee_keys: set[str] = set()
    for a in attendees:
        attendee_keys |= _attendee_match_keys(a)
    if not attendee_keys:
        return tasks
    out: list[dict[str, Any]] = []
    for t in tasks:
        owner_name = t.get("owner_display_name") or t.get("owner") or ""
        owner_uid = t.get("owner_user_id") or ""
        if not owner_name and not owner_uid:
            # No owner — keep (unassigned items are still worth
            # surfacing on the agenda).
            out.append(t)
            continue
        name_keys = _attendee_match_keys(str(owner_name))
        uid_keys = _attendee_match_keys(str(owner_uid))
        if attendee_keys & (name_keys | uid_keys):
            out.append(t)
    return out


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
    due_date_iso = t.due_date.isoformat() if t.due_date else None
    # `due_time` is a `time` object on the Task model (FR-CR-04-29).
    due_time = getattr(t, "due_time", None)
    due_time_str: str | None = None
    if due_time is not None:
        try:
            due_time_str = due_time.strftime("%H:%M")
        except Exception:  # noqa: BLE001
            due_time_str = None
    return {
        "id": t.id,
        "title": t.title or "",
        "description": (t.description or "")[:300],
        "status": getattr(t.status, "value", str(t.status)),
        "priority": getattr(t.priority, "value", str(t.priority)),
        "owner_display_name": t.owner_display_name or "",
        "owner_user_id": t.owner_user_id or "",
        "due_date": due_date_iso,
        "due_time": due_time_str,
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
    organizer_email: str | None = None,
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
    org_filter = (organizer_email or "").strip().lower() or None
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
        # FR-CR-05-167 — organizer filter. On Workspace / shared
        # calendars `organizer.email` is sometimes rewritten to
        # the calendar owner (a teammate's meeting copied into
        # your shared calendar still lists YOU as organizer).
        # Check BOTH `organizer.email` and `creator.email` and
        # require the operator email to match AT LEAST ONE of
        # them. `creator` keeps the original author and is the
        # right gate against teammates' events sneaking through.
        if org_filter:
            def _email_of(field: Any) -> str:
                if isinstance(field, dict):
                    return (field.get("email") or "").strip().lower()
                if isinstance(field, str):
                    return field.strip().lower()
                return ""

            org_email = _email_of(ev.get("organizer"))
            creator_email = _email_of(ev.get("creator"))
            if org_filter not in {org_email, creator_email}:
                continue
            # AND the operator must NOT be just a participant on
            # a teammate's meeting (the shared-calendar rewrite
            # case): if creator IS set and points elsewhere, drop
            # the event regardless of how `organizer` reads.
            if creator_email and creator_email != org_filter:
                continue
        # Skip already-posted.
        if svc.is_already_posted(session, calendar_event_id=ev_id):
            continue
        prior = find_prior_recordings(
            session, title=title, lookback_days=lookback_days, now=now,
        )
        if len(prior) < max(1, int(min_prior_meetings)):
            continue
        # FR-CR-05-192ab — open_tasks берём только из LAST prior
        # recording (operator-pinned 2026-05-22: для Fundraising daily
        # у нас 29 prior за 90 дней → 469 tasks → confusion). По
        # умолчанию last-only; для отката set env
        # `AGENDA_TASKS_FROM_LAST_PRIOR_ONLY=false` → вернётся
        # aggregate по всем prior.
        import os as _os
        _last_only = _os.environ.get(
            "AGENDA_TASKS_FROM_LAST_PRIOR_ONLY", "true",
        ).strip().lower() not in ("false", "0", "no", "off")
        if _last_only and prior:
            zoom_ids = [prior[0].zoom_id]
        else:
            zoom_ids = [r.zoom_id for r in prior]
        open_tasks = open_tasks_for_recordings(
            session, zoom_ids=zoom_ids,
        )
        rendered_tasks = [render_task_for_prompt(t) for t in open_tasks]
        resolved_attendees = _resolve_attendees(
            ev.get("attendees") or [], session,
        )
        # FR-CR-05-167 revert 2026-05-14: keep ALL open tasks for
        # the recurring title — operator wants the full status
        # picture, not a per-attendee subset. Earlier we filtered
        # by `attendee ∋ owner`; that was over-restrictive and
        # also leaked teammates' meetings when their attendees
        # accidentally overlapped.
        candidate = AgendaCandidate(
            calendar_event_id=ev_id,
            recurring_event_id=ev.get("recurring_event_id"),
            title=title,
            title_normalised=normalise_title(title),
            scheduled_start_at=start,
            description=ev.get("description"),
            attendees=resolved_attendees,
            prior_recordings=[
                render_recording_for_prompt(r) for r in prior
            ],
            open_tasks=rendered_tasks,
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
