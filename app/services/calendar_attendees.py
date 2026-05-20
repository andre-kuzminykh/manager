"""FR-CR-05-169 — Calendar-driven attendees resolution for meeting summaries.

Replaces (or supplements) LLM-from-transcript participant extraction
with the authoritative list pulled from the matching Google Calendar
event. Operator-pinned 2026-05-20:

    «zoom митинги должны относиться к календарю, и я из календаря
    вычленяю участников, мэплю их с таблицей людей и это выдаю в
    итоге — реальных людей что были на встрече».

Workflow:

  1. Take a `ZoomRecording` (or in future a `FirefliesTranscript`) row.
  2. Match it to one Google Calendar event by, in priority order:
       (a) URL match — `row.zoom_meeting_id` appears in
           `event.description` (Zoom join URLs contain the
           numeric meeting id);
       (b) Fuzzy match — `event.start` within ±15 min of
           `row.meeting_date` AND normalised(title) overlap is
           significant (substring containment of the normalised
           shorter title in the longer one).
  3. For each attendee on the chosen event, look up the email in a
     combined map of `team_members` + `employees` + `counterparties`
     (the last via the `personal_information.emails` field stored
     in `counterparty_attrs.attributes`). Unresolved emails kept
     with `source="unknown"` and `displayName` (or email) as label.
  4. Drop attendees with ``responseStatus="declined"``.
  5. Emit a dict with the resolved attendees list, the match method,
     and counters for diagnostics. Persist on
     `row.calendar_attendees`; the summary builder prefers it over
     the LLM-extracted `row.participants` when present.

Returns ``None`` when no event matches → caller falls back to the
existing LLM extraction.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.agenda.service import _build_email_to_name_map, normalise_title
from app.logging_setup import get_logger
from app.models import (  # type: ignore
    Counterparty,
    CounterpartyAttribute,
)

log = get_logger(__name__)


_FUZZY_TIME_WINDOW = timedelta(minutes=15)
_ZOOM_URL_RE = re.compile(r"zoom\.us/[a-z]+/(\d{5,})", re.IGNORECASE)


def _build_counterparty_email_map(session: Session) -> dict[str, str]:
    """Walk ``counterparty_attrs.attributes['personal_information']
    ['emails']`` across every counterparty and emit ``{email: name}``.

    The counterparty briefs pipeline writes its enriched person data
    here. Returns lower-cased emails. Best-effort — query errors
    return an empty map without breaking the resolver."""
    out: dict[str, str] = {}
    try:
        rows = (
            session.query(CounterpartyAttribute, Counterparty)
            .join(
                Counterparty,
                Counterparty.id == CounterpartyAttribute.counterparty_id,
            )
            .all()
        )
    except Exception as e:  # noqa: BLE001
        log.info(
            "calendar_attendees_counterparty_query_failed", error=str(e),
        )
        return out
    for attr, cp in rows:
        try:
            attributes = attr.attributes or {}
            pi = attributes.get("personal_information") or {}
            emails = pi.get("emails") or []
            if not isinstance(emails, list):
                continue
            for e in emails:
                if not isinstance(e, str):
                    continue
                key = e.strip().lower()
                if not key:
                    continue
                # First write wins — later attribute rows shouldn't
                # silently override an earlier resolution.
                if key not in out:
                    out[key] = (cp.name or "").strip()
        except Exception as e:  # noqa: BLE001
            log.info(
                "calendar_attendees_counterparty_attr_parse_failed",
                cp_id=getattr(cp, "id", None), error=str(e),
            )
    return out


def _extract_meeting_ids_from_description(description: str) -> set[str]:
    """Pull every Zoom meeting id out of an event description.
    Zoom join URLs look like ``https://zoom.us/j/<numeric_id>`` (or
    ``/wc/`` for web-client, ``/my/`` for personal). We extract the
    numeric tail so we can substring-match against
    ``row.zoom_meeting_id`` without worrying about scheme / path
    variants."""
    if not description:
        return set()
    return {m.group(1) for m in _ZOOM_URL_RE.finditer(description)}


def _event_title(event: dict[str, Any]) -> str:
    """Pull the event title regardless of which shape we got.
    `app.services.calendar_match.fetch_calendar_events_via_api`
    renames Calendar API's ``summary`` to ``title`` before
    returning; tests supply raw API events with ``summary``. Accept
    both so the same resolver works for prod + tests."""
    return (event.get("title") or event.get("summary") or "").strip()


def _coerce_event_start(event: dict[str, Any]) -> datetime | None:
    """Lift the event start time into a tz-aware ``datetime``.
    Handles both shapes:
      * wrapper format: ``event["start"]`` is a flat ISO string
        (`fetch_calendar_events_via_api`).
      * raw Calendar API: ``event["start"]`` is a dict with
        ``dateTime`` or ``date``."""
    s = event.get("start") or {}
    if isinstance(s, str):
        raw = s
    elif isinstance(s, dict):
        raw = s.get("dateTime") or s.get("date") or ""
    else:
        return None
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _title_fuzzy_overlap(a: str, b: str) -> bool:
    """Normalised-title containment in either direction. Catches
    «Fundraising daily» vs «Fundraising  Daily » and similar drift
    that ``normalise_title`` strips."""
    na = normalise_title(a or "")
    nb = normalise_title(b or "")
    if not na or not nb:
        return False
    return na in nb or nb in na


def _find_matching_event(
    events: list[dict[str, Any]],
    *,
    zoom_meeting_id: str | None,
    meeting_date: datetime | None,
    meeting_title: str | None,
) -> tuple[dict[str, Any] | None, str]:
    """Returns ``(event_dict_or_None, match_method)``. ``match_method``
    is ``"url"``, ``"fuzzy"`` or ``""`` when nothing matches."""
    if not events:
        return None, ""
    # 1. URL match — strongest signal.
    if zoom_meeting_id:
        zid = str(zoom_meeting_id).strip()
        if zid:
            for ev in events:
                ids = _extract_meeting_ids_from_description(
                    ev.get("description") or "",
                )
                if zid in ids:
                    return ev, "url"
    # 2. Fuzzy match — start time window + title overlap.
    if meeting_date is not None and meeting_title:
        for ev in events:
            ev_start = _coerce_event_start(ev)
            if ev_start is None:
                continue
            if abs(ev_start - meeting_date) > _FUZZY_TIME_WINDOW:
                continue
            if _title_fuzzy_overlap(meeting_title, _event_title(ev)):
                return ev, "fuzzy"
    return None, ""


def _resolve_attendee(
    item: dict[str, Any],
    *,
    email_to_team_name: dict[str, str],
    email_to_counterparty_name: dict[str, str],
) -> dict[str, Any]:
    """Build one resolved-attendee dict from a Calendar API attendee
    entry. Always returns a dict — `source="unknown"` when no match."""
    email = (item.get("email") or "").strip().lower()
    display = (item.get("displayName") or item.get("name") or "").strip()
    response_status = (item.get("responseStatus") or "needsAction").strip()

    resolved_name = ""
    source = "unknown"
    if email and email in email_to_team_name:
        resolved_name = email_to_team_name[email]
        # `_build_email_to_name_map` covers TeamMember + Employee +
        # hardcoded fallback. We can't distinguish team_member vs
        # employee without a second query — both are «internal», so
        # we collapse them under "team_member" in the resolved
        # payload. (The internal/external distinction is what
        # matters; the team_members vs employees split is
        # bookkeeping.)
        source = "team_member"
    elif email and email in email_to_counterparty_name:
        resolved_name = email_to_counterparty_name[email]
        source = "counterparty"
    if not resolved_name:
        resolved_name = display or email
    return {
        "email": email,
        "display_name": display,
        "resolved_name": resolved_name,
        "source": source,
        "response_status": response_status,
    }


def resolve_calendar_attendees_for_zoom(
    row: Any,
    session: Session,
    *,
    calendar_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Find the Calendar event for the given ``ZoomRecording`` and
    resolve its attendees. Returns ``{"event_id": str, "match_method":
    "url"|"fuzzy", "attendees": [...], "dropped_declined": int}`` or
    ``None`` when no event matched.

    ``calendar_events`` lets callers (tests, smoke harness) inject a
    fixed list. In production, leaving it as ``None`` triggers a
    Calendar API fetch via ``app.services.calendar_match`` — keeping
    that lazy import here so the test path doesn't drag in
    `google.*` deps. (Not yet wired — first integration in pipeline
    builds the event list once per ingest batch and passes it in.)
    """
    if calendar_events is None:
        calendar_events = []

    ev, match_method = _find_matching_event(
        calendar_events,
        zoom_meeting_id=getattr(row, "zoom_meeting_id", None),
        meeting_date=getattr(row, "meeting_date", None),
        meeting_title=getattr(row, "title", None),
    )
    if ev is None:
        return None

    raw_attendees = ev.get("attendees") or []
    if not isinstance(raw_attendees, list):
        raw_attendees = []

    email_to_team_name = _build_email_to_name_map(session)
    email_to_counterparty_name = _build_counterparty_email_map(session)

    resolved: list[dict[str, Any]] = []
    dropped_declined = 0
    for item in raw_attendees:
        if not isinstance(item, dict):
            continue
        if (item.get("responseStatus") or "").strip().lower() == "declined":
            dropped_declined += 1
            continue
        resolved.append(_resolve_attendee(
            item,
            email_to_team_name=email_to_team_name,
            email_to_counterparty_name=email_to_counterparty_name,
        ))

    result = {
        "event_id": ev.get("id") or "",
        "match_method": match_method,
        "attendees": resolved,
        "dropped_declined": dropped_declined,
        "resolved_count": sum(1 for a in resolved if a["source"] != "unknown"),
        "unknown_count": sum(1 for a in resolved if a["source"] == "unknown"),
    }
    log.info(
        "zoom_calendar_attendees_resolved",
        zoom_id=getattr(row, "zoom_id", None),
        event_id=result["event_id"],
        match_method=match_method,
        attendees_count=len(resolved),
        resolved_count=result["resolved_count"],
        unknown_count=result["unknown_count"],
        dropped_declined=dropped_declined,
    )
    return result


__all__ = [
    "resolve_calendar_attendees_for_zoom",
]
