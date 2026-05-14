"""FR-CR-05-166 — Calendar-less recurring detection.

When the operator can't (or won't) grant the bot full Calendar
access, we approximate «upcoming recurring meetings» by mining
``zoom_recordings`` for weekly patterns:

  * Group recordings by ``normalise_title``.
  * Sort each group ascending by ``meeting_date``.
  * Detect a weekly pattern: every consecutive delta ≈ 7 days
    (±1 day tolerance) AND same weekday AND same time-of-day
    within a configurable tolerance.
  * Predict the next instance = ``last + 7 days`` at the same
    wall-clock time-of-day on the same weekday.

Output is the same event shape the Calendar path returns
(`{id, title, start, end, attendees, description}`) so the rest of
the agenda pipeline doesn't care which source produced it.

Limitations (operator-pinned in the SPEC, FR-CR-05-166):
  * Weekly only — daily / bi-weekly / monthly cadences land in
    backlog. They're rare for the operator's current calendar.
  * Doesn't see ad-hoc reschedules — if you moved Tuesday's call
    to Wednesday IN CALENDAR, this code still predicts Tuesday.
  * Requires at least ``min_prior_meetings`` (default 2)
    recordings in the group — one-off meetings produce no pattern.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agenda.service import normalise_title
from app.logging_setup import get_logger
from app.models import ZoomRecording

log = get_logger(__name__)


@dataclass
class WeeklyPattern:
    """A weekly recurrence inferred from past zoom_recordings."""

    title: str
    title_normalised: str
    weekday: int        # 0 = Monday, 6 = Sunday — same as datetime.weekday()
    hour: int           # UTC hour of the typical start
    minute: int         # UTC minute of the typical start
    instances: int      # how many prior recordings agreed with the pattern
    last_instance_at: datetime  # tz-aware


def _hour_minute_close(
    a: time, b: time, tolerance_minutes: int = 30
) -> bool:
    """`a` and `b` are close enough that we treat them as the same
    «time-of-day» (operator might start a few minutes late)."""
    delta = abs(
        (a.hour * 60 + a.minute) - (b.hour * 60 + b.minute)
    )
    return delta <= tolerance_minutes


def _detect_weekly(
    recordings: list[ZoomRecording],
    *,
    weekday_tolerance_days: int = 1,
    time_tolerance_minutes: int = 30,
) -> WeeklyPattern | None:
    """Find a weekly pattern in `recordings` (already sorted asc by
    meeting_date). Returns None when fewer than 2 valid
    instances OR the deltas don't look weekly."""
    if len(recordings) < 2:
        return None

    valid_dates: list[datetime] = []
    for r in recordings:
        if r.meeting_date is None:
            continue
        dt = r.meeting_date
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        valid_dates.append(dt)
    if len(valid_dates) < 2:
        return None

    # All consecutive deltas should be ≈ 7 days.
    # Tolerance: a recording started 1 hour late is fine, but a
    # delta of 14 days means we skipped an iteration — still
    # weekly-ish, so accept up to ~10 days. A delta > 10 days
    # means the pattern broke.
    deltas = [
        (b - a).total_seconds() / 86400.0
        for a, b in zip(valid_dates, valid_dates[1:])
    ]
    # FR-CR-05-166: accept 6-10 days as «weekly». 14 days = skipped
    # iteration is questionable; for now require all deltas in
    # window.
    if not all(6.0 <= d <= 10.0 for d in deltas):
        return None

    last = valid_dates[-1]
    typical_weekday = last.weekday()
    typical_time = last.timetz()

    # Reject groups where the weekday wanders more than 1 day —
    # operator's recurring calls don't shift across the week.
    weekday_mismatches = sum(
        1 for dt in valid_dates
        if abs(dt.weekday() - typical_weekday) > weekday_tolerance_days
    )
    if weekday_mismatches >= len(valid_dates) // 2:
        return None

    # Time-of-day stability — at least half the instances should
    # start within `time_tolerance_minutes` of the typical time.
    time_matches = sum(
        1 for dt in valid_dates
        if _hour_minute_close(dt.timetz(), typical_time, time_tolerance_minutes)
    )
    if time_matches < len(valid_dates) // 2 + 1:
        return None

    return WeeklyPattern(
        title=recordings[-1].title or "",
        title_normalised=normalise_title(recordings[-1].title or ""),
        weekday=typical_weekday,
        hour=typical_time.hour,
        minute=typical_time.minute,
        instances=len(valid_dates),
        last_instance_at=last,
    )


def _next_instance_at(pattern: WeeklyPattern, *, now: datetime) -> datetime:
    """Predict the next start_dt for a weekly pattern.

    Returns the first candidate `>= now`. Adding 7-day increments
    to ``pattern.last_instance_at`` (normalised to the typical
    time-of-day) gives the chain; we step until the candidate
    catches up with `now`. If `now` exactly matches a predicted
    instance, that instance is returned (so an agenda tick fired
    at `now = start` still picks up the event).
    """
    candidate = pattern.last_instance_at.replace(
        hour=pattern.hour, minute=pattern.minute, second=0, microsecond=0,
    )
    while candidate < now:
        candidate += timedelta(days=7)
    return candidate


def predict_upcoming_events(
    session: Session,
    *,
    target_dt: datetime,
    window_minutes: int,
    lookback_days: int = 90,
    min_prior_meetings: int = 2,
) -> list[dict[str, Any]]:
    """Return a list of synthetic «events» for recurring patterns
    whose next predicted instance falls within
    ``[target_dt - window, target_dt + window]``.

    Same return shape as ``fetch_calendar_events_via_api``:
        {id, title, start, end, attendees, description, ...}

    The `id` is a stable synthesised key — same as the runner's
    `agenda_synth:<title>:<start_iso>` so the existing idempotency
    row in ``meeting_agendas`` works without changes.

    Failure is silent — empty list on any DB error so the runner
    tick never crashes.
    """
    if target_dt.tzinfo is None:
        target_dt = target_dt.replace(tzinfo=timezone.utc)
    window = max(1, int(window_minutes))
    earliest = target_dt - timedelta(minutes=window)
    latest = target_dt + timedelta(minutes=window)

    cutoff = target_dt - timedelta(days=max(1, int(lookback_days)))

    try:
        stmt = (
            select(ZoomRecording)
            .where(ZoomRecording.meeting_date.is_not(None))
            .where(ZoomRecording.meeting_date >= cutoff)
            .where(ZoomRecording.meeting_date < target_dt)
            .order_by(ZoomRecording.meeting_date.asc())
        )
        rows = list(session.execute(stmt).scalars().all())
    except Exception as e:  # noqa: BLE001
        log.warning("zoom_pattern_query_failed", error=str(e))
        return []

    grouped: dict[str, list[ZoomRecording]] = defaultdict(list)
    for r in rows:
        key = normalise_title(r.title or "")
        if not key:
            continue
        grouped[key].append(r)

    events: list[dict[str, Any]] = []
    for key, group in grouped.items():
        if len(group) < max(2, int(min_prior_meetings)):
            continue
        pattern = _detect_weekly(group)
        if pattern is None:
            continue
        predicted = _next_instance_at(pattern, now=target_dt)
        if not (earliest <= predicted <= latest):
            continue
        synth_id = f"agenda_synth:{key}:{predicted.isoformat()}"
        events.append(
            {
                "id": synth_id,
                "title": pattern.title,
                "start": predicted,
                "end": predicted + timedelta(minutes=30),  # heuristic
                "description": "",
                "attendees": [],
                "recurring_event_id": None,
            }
        )

    log.info(
        "zoom_pattern_predicted",
        groups=len(grouped),
        matched=len(events),
        target_iso=target_dt.isoformat(),
        window_minutes=window,
    )
    return events


__all__ = [
    "WeeklyPattern",
    "predict_upcoming_events",
]
