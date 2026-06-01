"""FR-CR-05-235 — agenda must not be blanked out by a failed/empty
prior recording.

WHY (operator incident 2026-06-01):
  The «Дмитрий Седов» 1-1 recurs ~weekly. Its agenda came out with
  ZERO tasks even though the meeting clearly produces action items.
  Root cause: the newest prior Zoom recording (2026-05-20, 20 min)
  came back with NEAR-SILENT audio → Whisper produced a 14-char
  transcript → 0 tasks extracted, no Doc. The agenda sources its
  open-tasks from the *newest* prior recording only (FR-CR-05-192ab,
  introduced so «Fundraising daily» wouldn't aggregate 29 priors →
  469 tasks). So a single broken recording silently emptied the
  agenda, while the real substantive meeting (2026-05-19, 3197-char
  transcript, 9 tasks) sat one slot older and was ignored.

CONTRACT (this is the specification — keep prod in sync or this
breaks):
  In `build_candidates`, last-prior-only mode (the default), the
  recording chosen to source `open_tasks` is the NEWEST prior whose
  transcript is USABLE — i.e. `is_transcript_unsummarizable(...)`
  returns False (≥800 chars, not garbage). Thin/empty recordings are
  skipped. If NO prior is usable, fall back to `prior[0]` (genuine
  empty history → empty agenda is acceptable, no crash).

  Invariants that MUST hold (regression guards):
   1. The `min_prior_meetings` gate still counts ALL priors — a
      meeting is NEVER dropped just because its newest recording is
      thin. (A meeting with 1 usable + 1 thin prior and
      min_prior_meetings=2 still qualifies.)
   2. Bounded cost: exactly ONE recording feeds open_tasks (no
      aggregate) — the FR-CR-05-192ab «no 469 tasks» guarantee.
   3. When the newest prior is already usable, it is used unchanged
      (no behaviour change for healthy meetings like Fundraising
      daily).
   4. `AGENDA_TASKS_FROM_LAST_PRIOR_ONLY=false` still returns the
      aggregate over all priors (escape-hatch preserved).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.agenda.service import build_candidates, normalise_title
from app.models import Task, TaskPriority, TaskStatus, ZoomRecording

_NOW = datetime(2026, 6, 1, 9, 0, 0, tzinfo=timezone.utc)
_GOOD_TRANSCRIPT = "Дмитрий: " + ("обсудили план найма и бюджет. " * 60)  # ≥800
_THIN_TRANSCRIPT = "пара слов"  # <800 → unsummarizable


def _evt(id_: str, title: str, when: datetime, **extras):
    return {"id": id_, "title": title, "start": when, **extras}


def _zoom(session, *, zoom_id, title, when, transcript):
    session.add(
        ZoomRecording(
            zoom_id=zoom_id, title=title, meeting_date=when,
            transcript_text=transcript,
        )
    )


def _budget_task(session, *, zoom_id, title):
    """An OPEN, important-direction task bound to a recording — the
    kind the agenda surfaces (direction filter keeps budget/etc.)."""
    session.add(
        Task(
            title=title,
            status=TaskStatus.backlog,
            priority=TaskPriority.high,
            source_kind="zoom",
            source_conversation_id=zoom_id,
            extra={"direction": "budget"},
        )
    )


def test_skips_thin_newest_prior_and_uses_last_substantive(session):
    """The incident: newest prior is empty (silent audio), the one
    before it is substantive with tasks → agenda must carry THOSE
    tasks, not 0."""
    # newest, thin/empty (the broken 05-20 analogue)
    _zoom(session, zoom_id="z_empty", title="Дмитрий Седов",
          when=_NOW - timedelta(days=1), transcript=_THIN_TRANSCRIPT)
    # older, substantive with a real open task (the 05-19 analogue)
    _zoom(session, zoom_id="z_good", title="Дмитрий Седов",
          when=_NOW - timedelta(days=2), transcript=_GOOD_TRANSCRIPT)
    _budget_task(session, zoom_id="z_good", title="Approve hiring budget")
    # a task on the empty one must NOT exist; prove tasks come from z_good
    session.flush()

    cands = build_candidates(
        session, events=[_evt("ev", "Дмитрий Седов", _NOW + timedelta(minutes=10))],
        lookback_days=90, min_prior_meetings=2, now=_NOW,
    )
    assert len(cands) == 1, "meeting must still qualify (2 priors counted)"
    titles = [t["title"] for t in cands[0].open_tasks]
    assert titles == ["Approve hiring budget"], (
        "open_tasks must be sourced from the last SUBSTANTIVE prior, "
        f"not the empty newest one; got {titles!r}"
    )


def test_healthy_newest_prior_is_used_unchanged(session):
    """No regression: when the newest prior is usable, its tasks are
    used (we don't reach back further)."""
    _zoom(session, zoom_id="z_new", title="Fundraising daily",
          when=_NOW - timedelta(days=1), transcript=_GOOD_TRANSCRIPT)
    _zoom(session, zoom_id="z_old", title="Fundraising daily",
          when=_NOW - timedelta(days=2), transcript=_GOOD_TRANSCRIPT)
    _budget_task(session, zoom_id="z_new", title="Today task")
    _budget_task(session, zoom_id="z_old", title="Yesterday task")
    session.flush()

    cands = build_candidates(
        session, events=[_evt("ev", "Fundraising daily", _NOW + timedelta(minutes=10))],
        lookback_days=90, min_prior_meetings=1, now=_NOW,
    )
    titles = [t["title"] for t in cands[0].open_tasks]
    assert titles == ["Today task"], (
        f"healthy newest prior should be used as-is; got {titles!r}"
    )


def test_all_thin_priors_fall_back_to_newest_no_crash(session):
    """Degenerate: every prior is thin. Must not crash; falls back to
    prior[0] (empty agenda is acceptable)."""
    _zoom(session, zoom_id="z1", title="Дмитрий Седов",
          when=_NOW - timedelta(days=1), transcript=_THIN_TRANSCRIPT)
    _zoom(session, zoom_id="z2", title="Дмитрий Седов",
          when=_NOW - timedelta(days=2), transcript=_THIN_TRANSCRIPT)
    session.flush()

    cands = build_candidates(
        session, events=[_evt("ev", "Дмитрий Седов", _NOW + timedelta(minutes=10))],
        lookback_days=90, min_prior_meetings=2, now=_NOW,
    )
    assert len(cands) == 1
    assert cands[0].open_tasks == []  # no tasks anywhere → empty, but no error


def test_bounded_single_recording_not_aggregate(session):
    """FR-CR-05-192ab invariant preserved: even with several
    substantive priors each carrying tasks, only ONE recording feeds
    open_tasks (the newest usable) — never the union."""
    _zoom(session, zoom_id="z_new", title="Дмитрий Седов",
          when=_NOW - timedelta(days=1), transcript=_GOOD_TRANSCRIPT)
    _zoom(session, zoom_id="z_old", title="Дмитрий Седов",
          when=_NOW - timedelta(days=2), transcript=_GOOD_TRANSCRIPT)
    _budget_task(session, zoom_id="z_new", title="New A")
    _budget_task(session, zoom_id="z_old", title="Old B")
    session.flush()

    cands = build_candidates(
        session, events=[_evt("ev", "Дмитрий Седов", _NOW + timedelta(minutes=10))],
        lookback_days=90, min_prior_meetings=1, now=_NOW,
    )
    titles = {t["title"] for t in cands[0].open_tasks}
    assert titles == {"New A"}, f"must be single newest-usable recording, got {titles}"
    assert normalise_title("Дмитрий Седов") == "дмитрии седов"  # key sanity
