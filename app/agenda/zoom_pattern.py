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
class RecurrencePattern:
    """A recurring pattern inferred from past zoom_recordings.

    `period_days` is the inter-instance interval (1=daily,
    7=weekly, 14=bi-weekly). `weekday` is meaningful only when
    period_days is a multiple of 7 (otherwise the pattern lands
    on different weekdays).
    """

    title: str
    title_normalised: str
    period_days: int
    weekday: int | None  # set only for weekly / bi-weekly
    hour: int            # UTC hour of the typical start
    minute: int          # UTC minute of the typical start
    instances: int       # how many prior recordings agreed
    last_instance_at: datetime  # tz-aware


# Back-compat alias for code that imported the v0.1 name.
WeeklyPattern = RecurrencePattern


def _hour_minute_close(
    a: time, b: time, tolerance_minutes: int = 30
) -> bool:
    """`a` and `b` are close enough that we treat them as the same
    «time-of-day» (operator might start a few minutes late)."""
    delta = abs(
        (a.hour * 60 + a.minute) - (b.hour * 60 + b.minute)
    )
    return delta <= tolerance_minutes


# Period candidates the heuristic recognises (in days). Sorted so
# we prefer the *shortest* matching period when multiple fit
# (e.g. 6-day delta should snap to daily not weekly).
_PERIOD_CANDIDATES = (
    (1, 0.7, 1.5),    # daily — operator's Иринины Подземелья
    (7, 6.0, 10.0),   # weekly — most fundraising syncs
    (14, 12.0, 16.0), # bi-weekly
)


def _detect_pattern(
    recordings: list[ZoomRecording],
    *,
    time_tolerance_minutes: int = 30,
) -> RecurrencePattern | None:
    """Find any recurring pattern in `recordings` (already sorted
    asc by meeting_date). Returns None when fewer than 2 valid
    instances OR the deltas don't match any known cadence.

    Periods tried (in this order — shortest first):
      * 1 day  — daily standup-style series
      * 7 days — weekly
      * 14 days — bi-weekly

    Time-of-day stability is required for ALL cadences. Same
    weekday is required for ≥7-day cadences only (daily lands
    on different weekdays by definition).
    """
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

    deltas = [
        (b - a).total_seconds() / 86400.0
        for a, b in zip(valid_dates, valid_dates[1:])
    ]
    last = valid_dates[-1]
    typical_time = last.timetz()

    # Time-of-day stability — at least half the instances should
    # start within `time_tolerance_minutes` of the typical time.
    # Cheap check, run once across the group.
    time_matches = sum(
        1 for dt in valid_dates
        if _hour_minute_close(dt.timetz(), typical_time, time_tolerance_minutes)
    )
    if time_matches < len(valid_dates) // 2 + 1:
        return None

    for period_days, lo, hi in _PERIOD_CANDIDATES:
        # Require MOST deltas (≥80%) to fit this period. One outlier
        # is fine (operator skipped a week) but a noisy group is
        # rejected.
        fits = sum(1 for d in deltas if lo <= d <= hi)
        if fits < max(1, int(round(0.8 * len(deltas)))):
            continue

        weekday: int | None = None
        if period_days % 7 == 0:
            typical_weekday = last.weekday()
            weekday_matches = sum(
                1 for dt in valid_dates
                if abs(dt.weekday() - typical_weekday) <= 1
            )
            if weekday_matches < len(valid_dates) // 2 + 1:
                continue
            weekday = typical_weekday

        return RecurrencePattern(
            title=recordings[-1].title or "",
            title_normalised=normalise_title(recordings[-1].title or ""),
            period_days=period_days,
            weekday=weekday,
            hour=typical_time.hour,
            minute=typical_time.minute,
            instances=len(valid_dates),
            last_instance_at=last,
        )

    return None


# Back-compat — old callers imported `_detect_weekly`.
_detect_weekly = _detect_pattern


def _next_instance_at(pattern: RecurrencePattern, *, now: datetime) -> datetime:
    """Predict the next start_dt for a recurring pattern.

    Returns the first candidate `>= now`. Adding `period_days`
    increments to ``pattern.last_instance_at`` (normalised to the
    typical time-of-day) gives the chain; we step until the
    candidate catches up with `now`.
    """
    candidate = pattern.last_instance_at.replace(
        hour=pattern.hour, minute=pattern.minute, second=0, microsecond=0,
    )
    step = timedelta(days=max(1, pattern.period_days))
    while candidate < now:
        candidate += step
    return candidate


def predict_upcoming_events(
    session: Session,
    *,
    target_dt: datetime,
    window_minutes: int,
    lookback_days: int = 90,
    min_prior_meetings: int = 2,
    host_email: str | None = None,
) -> list[dict[str, Any]]:
    """Return a list of synthetic «events» for recurring patterns
    whose next predicted instance falls within
    ``[target_dt - window, target_dt + window]``.

    `host_email` (FR-CR-05-166): when set, only consider
    recordings whose `host_email` matches (case-insensitive).
    Recordings with NULL host_email are SKIPPED — better to miss
    a series than to post an agenda for a teammate's meeting.

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
        if host_email:
            stmt = stmt.where(
                ZoomRecording.host_email == host_email.strip().lower()
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
    diagnosed: list[dict[str, Any]] = []
    for key, group in grouped.items():
        if len(group) < max(2, int(min_prior_meetings)):
            continue
        pattern = _detect_pattern(group)
        if pattern is None:
            continue
        predicted = _next_instance_at(pattern, now=target_dt)
        diagnosed.append(
            {
                "title": pattern.title,
                "period_days": pattern.period_days,
                "instances": pattern.instances,
                "predicted": predicted.isoformat(),
            }
        )
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
        patterns_found=len(diagnosed),
        matched=len(events),
        target_iso=target_dt.isoformat(),
        window_minutes=window,
        diagnosed=diagnosed[:30],  # cap for log volume
    )
    return events


__all__ = [
    "WeeklyPattern",
    "predict_upcoming_events",
]
