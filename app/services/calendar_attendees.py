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

import json
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
    allow_time_only: bool = False,
) -> tuple[dict[str, Any] | None, str]:
    """Returns ``(event_dict_or_None, match_method)``. ``match_method``
    is ``"url"``, ``"fuzzy"``, ``"time"`` or ``""`` when nothing matches."""
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
    # 3. FR-CR-05-208 — time-only fallback. Fireflies RENAMES meetings
    # (e.g. «Mitsubishi: роботы…» for a calendar event titled «Kodai
    # Yamagishi … Zoom call»), so title overlap fails. A person is in one
    # meeting at a time, so the CLOSEST calendar event in the time window
    # that actually has attendees is the meeting. Events with no attendees
    # (working-location / focus blocks like «bedtime», «Set your working
    # location») are skipped — they're not meetings.
    if allow_time_only and meeting_date is not None:
        best: dict[str, Any] | None = None
        best_delta: timedelta | None = None
        for ev in events:
            ev_start = _coerce_event_start(ev)
            if ev_start is None:
                continue
            delta = abs(ev_start - meeting_date)
            if delta > _FUZZY_TIME_WINDOW:
                continue
            atts = ev.get("attendees")
            if not (isinstance(atts, list) and any(
                isinstance(a, dict) and (a.get("email") or a.get("displayName"))
                for a in atts
            )):
                continue
            if best_delta is None or delta < best_delta:
                best, best_delta = ev, delta
        if best is not None:
            return best, "time"
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
    allow_time_only: bool = False,
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
        allow_time_only=allow_time_only,
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


def resolve_calendar_attendees_for_fireflies(
    row: Any,
    session: Session,
    *,
    calendar_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """FR-CR-05-208 — Fireflies counterpart. Fireflies generates its OWN
    semantic meeting title (« Mitsubishi: роботы…»), which never matches the
    calendar event's title, so title-based fuzzy matching always fails and
    the meta block falls back to Fireflies' unreliable participant list
    (often the internal team). Match the calendar event by TIME proximity
    instead (``allow_time_only=True``) so the authoritative invitee list is
    used. Returns the same shape as the Zoom resolver, or ``None``."""
    return resolve_calendar_attendees_for_zoom(
        row, session, calendar_events=calendar_events, allow_time_only=True,
    )


def _normalise_name_for_match(name: str) -> set[str]:
    """Return a set of lowercased word-tokens for fuzzy matching.
    «Артем Соколов» → {"артем", "соколов"}; «Sokolov, Artem» →
    {"sokolov", "artem"}. Single-char tokens dropped (initials)."""
    import re as _re

    toks = _re.findall(r"\w+", (name or "").lower(), flags=_re.UNICODE)
    return {t for t in toks if len(t) > 1}


def reconcile_team_attendees(
    attendees: list[dict[str, Any]] | None,
    present_names: list[str] | None,
    *,
    keep_emails: "set[str] | list[str] | None" = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """FR-CR-05-253 — Fireflies counterpart of Zoom's join-time reconcile.

    Fireflies exposes no authoritative «who actually joined» list, so a
    calendar invitee who never showed (Kima: Jochen Rudat, invited but absent)
    would surface in the «Участники:» line. The only attendance signal FF has
    is the LLM-extracted speaker list (``row.participants``) — who actually
    spoke / was addressed in the transcript.

    Rule (conservative — only drop when we can VERIFY absence):
      * ``source == "team_member"`` → keep only if a name token overlaps the
        present-speaker tokens; otherwise DROP (invited teammate, no-show).
      * any other source (counterparty / unknown / external) → KEEP — we can't
        verify externals via team-speaker extraction, so we never drop them.
      * an attendee whose email is in ``keep_emails`` (the operator) → KEEP.

    Returns ``(kept, dropped)``. Pure / no I/O.
    """
    present_tok: set[str] = set()
    for n in present_names or []:
        present_tok |= _normalise_name_for_match(n if isinstance(n, str) else "")
    keep_cf = {(e or "").strip().lower() for e in (keep_emails or set()) if e}
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for a in attendees or []:
        if not isinstance(a, dict):
            kept.append(a)
            continue
        email = (a.get("email") or "").strip().lower()
        source = (a.get("source") or "").strip()
        if source != "team_member" or (email and email in keep_cf):
            kept.append(a)
            continue
        name = a.get("resolved_name") or a.get("display_name") or ""
        toks = _normalise_name_for_match(name)
        if toks and (toks & present_tok):
            kept.append(a)
        else:
            dropped.append(a)
    return kept, dropped


def _llm_reconcile_unmatched(
    *,
    unmatched_calendar: list[dict[str, Any]],
    unmatched_zoom: list[dict[str, str]],
    openai_client: Any,
    model: str,
) -> dict[int, int]:
    """Ask a small OpenAI model to match unmatched Calendar invitees
    with unmatched Zoom participants. Returns ``{calendar_idx:
    zoom_idx}`` for each match the model is confident in.

    Cheap single call — capped at ~20 candidates per side; beyond
    that we'd want a real entity-resolution pipeline.
    """
    if not unmatched_calendar or not unmatched_zoom:
        return {}
    if not openai_client:
        return {}
    cal_payload = [
        {
            "idx": i,
            "resolved_name": (a.get("resolved_name") or "").strip(),
            "display_name": (a.get("display_name") or "").strip(),
            "email": (a.get("email") or "").strip(),
        }
        for i, a in enumerate(unmatched_calendar[:20])
    ]
    zoom_payload = [
        {
            "idx": i,
            "user_name": (p.get("user_name") or "").strip(),
            "user_email": (p.get("user_email") or "").strip(),
        }
        for i, p in enumerate(unmatched_zoom[:20])
    ]
    system = (
        "You are an entity-matcher. The user gives you two lists: "
        "CALENDAR invitees and ZOOM participants for the SAME "
        "meeting. Some Calendar invitees joined under a different "
        "name / personal email in Zoom. Your job is to pair them "
        "up. Only pair a Calendar invitee with a Zoom participant "
        "when you are CONFIDENT they are the same person — partial "
        "name match (same first OR last name), or strongly similar "
        "transliterations (Ирина / Irina, Артем / Artem). Reply "
        "with ONE valid JSON object: {\"matches\": [{\"cal_idx\": "
        "<int>, \"zoom_idx\": <int>}, ...]}. Do NOT match by guess; "
        "leave unmatched pairs out."
    )
    user_text = json.dumps(
        {"calendar": cal_payload, "zoom": zoom_payload},
        ensure_ascii=False, indent=2,
    )
    try:
        resp = openai_client.chat.completions.create(
            model=model,
            max_tokens=512,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ],
        )
        content = (resp.choices[0].message.content or "").strip()
    except Exception as e:  # noqa: BLE001
        log.warning(
            "zoom_attendee_reconcile_llm_failed",
            error=str(e), error_type=type(e).__name__,
        )
        return {}
    try:
        parsed = json.loads(content)
    except Exception:  # noqa: BLE001
        log.warning(
            "zoom_attendee_reconcile_llm_parse_failed", content=content[:200],
        )
        return {}
    out: dict[int, int] = {}
    for m in (parsed.get("matches") or []):
        try:
            ci = int(m.get("cal_idx"))
            zi = int(m.get("zoom_idx"))
        except (TypeError, ValueError):
            continue
        if 0 <= ci < len(unmatched_calendar) and 0 <= zi < len(unmatched_zoom):
            # First match wins — don't let the LLM double-assign.
            if ci not in out and zi not in out.values():
                out[ci] = zi
    return out


def reconcile_with_zoom_participants(
    *,
    calendar_attendees: list[dict[str, Any]],
    zoom_participants: list[dict[str, str]],
    openai_client: Any | None = None,
    llm_model: str = "gpt-4o-mini",
    email_resolver: Any | None = None,
) -> dict[str, Any]:
    """FR-CR-05-172 — cross-reference Calendar invitees against who
    actually joined Zoom. Returns

        {
          "attendees": [...everybody who actually was on the call...],
          "unmatched_calendar": [...invited but didn't join...],
          "method_breakdown": {"email":N, "fuzzy":N, "llm":N, "zoom_only":N},
          "llm_used": bool,
        }

    Final ``attendees`` list contains:
      * every Calendar invitee whose presence in Zoom we could
        prove (email / fuzzy name / LLM match), AND
      * every Zoom participant who joined the call BUT was NOT on
        the Calendar invite (operator-pinned 2026-05-20: «к зуму
        может кто-то подключиться кого нет в встрече приглашенных
        тоже такой вариант»). These carry
        ``zoom_join_method="zoom_only"``.

    Calendar invitees who didn't join Zoom are dropped from
    ``attendees`` and surfaced under ``unmatched_calendar`` for
    diagnostics.

    ``email_resolver`` (optional callable) maps a lowercase email
    to ``{resolved_name, source}`` — used to resolve Zoom-only
    participants against `team_members` / `counterparties` even
    when they weren't on the Calendar invite. When not provided,
    Zoom-only attendees keep their raw Zoom user_name and
    ``source="unknown"``.
    """
    if not calendar_attendees and not zoom_participants:
        return {
            "attendees": [],
            "unmatched_calendar": [],
            "method_breakdown": {"email": 0, "fuzzy": 0, "llm": 0, "zoom_only": 0},
            "llm_used": False,
        }
    if calendar_attendees and not zoom_participants:
        # No Zoom data — can't filter. Return calendar list as-is
        # so the caller's downstream rendering is unaffected.
        return {
            "attendees": list(calendar_attendees),
            "unmatched_calendar": [],
            "method_breakdown": {"email": 0, "fuzzy": 0, "llm": 0, "zoom_only": 0},
            "llm_used": False,
        }

    # 1. Direct email match (cheapest).
    cal_used: set[int] = set()
    zoom_used: set[int] = set()
    matches: list[tuple[int, int, str]] = []  # (cal_idx, zoom_idx, method)
    zoom_by_email: dict[str, int] = {}
    for zi, p in enumerate(zoom_participants):
        e = (p.get("user_email") or "").strip().lower()
        if e and e not in zoom_by_email:
            zoom_by_email[e] = zi
    for ci, a in enumerate(calendar_attendees):
        e = (a.get("email") or "").strip().lower()
        if e and e in zoom_by_email:
            zi = zoom_by_email[e]
            if zi not in zoom_used:
                matches.append((ci, zi, "email"))
                cal_used.add(ci)
                zoom_used.add(zi)

    # 2. Fuzzy name match on what's left (no LLM).
    cal_tokens: dict[int, set[str]] = {}
    for ci, a in enumerate(calendar_attendees):
        if ci in cal_used:
            continue
        toks = (
            _normalise_name_for_match(a.get("resolved_name") or "")
            | _normalise_name_for_match(a.get("display_name") or "")
        )
        if toks:
            cal_tokens[ci] = toks
    for zi, p in enumerate(zoom_participants):
        if zi in zoom_used:
            continue
        z_toks = _normalise_name_for_match(p.get("user_name") or "")
        if not z_toks:
            continue
        for ci, c_toks in cal_tokens.items():
            if ci in cal_used:
                continue
            if c_toks & z_toks:  # any shared word-token wins
                matches.append((ci, zi, "fuzzy"))
                cal_used.add(ci)
                zoom_used.add(zi)
                break

    # 3. LLM reconcile for stubborn unmatched pairs (only when both
    # sides have leftovers — no point calling the LLM otherwise).
    unmatched_cal = [
        calendar_attendees[i] for i in range(len(calendar_attendees))
        if i not in cal_used
    ]
    unmatched_zoom = [
        zoom_participants[i] for i in range(len(zoom_participants))
        if i not in zoom_used
    ]
    llm_used = False
    llm_matches: dict[int, int] = {}
    if unmatched_cal and unmatched_zoom and openai_client is not None:
        # Rebuild the unmatched lists with the SAME ordering as the
        # caller's; need to track original indexes.
        cal_orig_idx = [
            i for i in range(len(calendar_attendees)) if i not in cal_used
        ]
        zoom_orig_idx = [
            i for i in range(len(zoom_participants)) if i not in zoom_used
        ]
        llm_matches = _llm_reconcile_unmatched(
            unmatched_calendar=unmatched_cal,
            unmatched_zoom=unmatched_zoom,
            openai_client=openai_client,
            model=llm_model,
        )
        if llm_matches:
            llm_used = True
            for sub_ci, sub_zi in llm_matches.items():
                ci = cal_orig_idx[sub_ci]
                zi = zoom_orig_idx[sub_zi]
                if ci in cal_used or zi in zoom_used:
                    continue
                matches.append((ci, zi, "llm"))
                cal_used.add(ci)
                zoom_used.add(zi)

    # Assemble final attendees list — everybody who was actually
    # on the call (matched Calendar invitees + Zoom-only joiners).
    joined: list[dict[str, Any]] = []
    method_count = {"email": 0, "fuzzy": 0, "llm": 0, "zoom_only": 0}
    # 1) Calendar invitees who joined, in Calendar order for stability.
    for ci, zi, method in sorted(matches, key=lambda x: x[0]):
        method_count[method] = method_count.get(method, 0) + 1
        rec = dict(calendar_attendees[ci])
        rec["zoom_join_method"] = method
        zp = zoom_participants[zi]
        rec.setdefault("zoom_user_name", zp.get("user_name"))
        rec.setdefault("zoom_user_email", zp.get("user_email"))
        joined.append(rec)
    # 2) Zoom-only participants — joined but not on the Calendar
    # invite. Try to resolve their Zoom email against the operator's
    # people tables; otherwise surface the raw Zoom user_name with
    # source="unknown" so the operator sees who it was.
    for zi in range(len(zoom_participants)):
        if zi in zoom_used:
            continue
        zp = zoom_participants[zi]
        email = (zp.get("user_email") or "").strip().lower()
        user_name = (zp.get("user_name") or "").strip()
        resolved_name = ""
        source = "unknown"
        if email_resolver is not None and email:
            try:
                resolved = email_resolver(email) or None
            except Exception as e:  # noqa: BLE001
                log.info(
                    "zoom_attendee_zoom_only_resolver_failed",
                    email=email, error=str(e),
                )
                resolved = None
            if resolved:
                resolved_name = (resolved.get("resolved_name") or "").strip()
                source = (resolved.get("source") or "unknown").strip()
        if not resolved_name:
            resolved_name = user_name or email
        method_count["zoom_only"] += 1
        joined.append({
            "email": email,
            "display_name": user_name,
            "resolved_name": resolved_name,
            "source": source,
            "response_status": "joined_zoom_only",
            "zoom_join_method": "zoom_only",
            "zoom_user_name": user_name,
            "zoom_user_email": email,
        })
    final_unmatched_cal = [
        calendar_attendees[i] for i in range(len(calendar_attendees))
        if i not in cal_used
    ]
    log.info(
        "zoom_attendee_reconcile_done",
        joined_count=len(joined),
        unmatched_calendar=len(final_unmatched_cal),
        method_breakdown=method_count,
        llm_used=llm_used,
    )
    return {
        "attendees": joined,
        "unmatched_calendar": final_unmatched_cal,
        "method_breakdown": method_count,
        "llm_used": llm_used,
    }


__all__ = [
    "resolve_calendar_attendees_for_zoom",
    "resolve_calendar_attendees_for_fireflies",
    "reconcile_with_zoom_participants",
]
