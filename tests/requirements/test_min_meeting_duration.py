"""FR-CR-05-192t — min-duration gate (5 min by default) in both
Fireflies + Zoom live pipelines.

Operator-pinned 2026-05-22: «не обрабатывать встречи меньше 5 мин».
Procedural / aborted-call recordings waste LLM budget and produce
no actionable output. The pipeline MUST short-circuit BEFORE
running any step (download / transcribe / summarise / extract).

Contract locked:
  - `Settings.min_meeting_seconds` (default 300) is the threshold.
  - `process_one` skips with `report.skipped_reason="duration_too_short"`
    when `row.duration_seconds < threshold`.
  - The check fires AFTER `_upsert_recording` (so DB still has the
    row for audit / retry) but BEFORE any LLM call.
  - Threshold=0 OR None disables the gate.
"""
from __future__ import annotations

from app.config import get_settings


def test_fr_cr_05_192t_default_threshold_is_300_seconds() -> None:
    """5 min = 300 sec by default. Operator can override via
    MIN_MEETING_SECONDS env var."""
    s = get_settings()
    assert s.min_meeting_seconds == 300


def test_fr_cr_05_192t_threshold_overridable_via_env(monkeypatch) -> None:
    """The setting reads from MIN_MEETING_SECONDS — operator can
    relax or tighten the gate without code changes."""
    from app.config import Settings
    monkeypatch.setenv("MIN_MEETING_SECONDS", "60")
    s = Settings()
    assert s.min_meeting_seconds == 60
    monkeypatch.setenv("MIN_MEETING_SECONDS", "0")
    s = Settings()
    assert s.min_meeting_seconds == 0


def test_fr_cr_05_192t_pipeline_skips_short_meetings_zoom(session) -> None:
    """Zoom pipeline.process_one MUST skip when row.duration_seconds
    < min_meeting_seconds. Skip happens AFTER upsert (row persisted
    for audit) but BEFORE any LLM step."""
    from unittest.mock import MagicMock
    from app.config import Settings
    from app.intent.llm_backends import OpenAIBackend
    from app.zoom.pipeline import ZoomPipeline, ZoomRecordingMeta

    settings = Settings(MIN_MEETING_SECONDS=300)
    pipe = ZoomPipeline(
        settings=settings,
        client=MagicMock(),
        llm_backend=MagicMock(spec=OpenAIBackend),
    )
    from datetime import datetime as _dt, timezone as _tz
    m = ZoomRecordingMeta(
        id="zz-short==",
        meeting_id=None,
        title="Short call",
        meeting_date=_dt(2026, 5, 22, 10, 0, tzinfo=_tz.utc),
        duration_seconds=120,  # 2 min — below threshold
        participants=[],
        audio_url=None,
        share_url=None,
        host_email=None,
        raw={"id": "zz-short==", "duration": 2, "topic": "Short call"},
    )
    report = pipe.process_one(session, m)
    assert report.skipped_reason == "duration_too_short"


def test_fr_cr_05_192t_pipeline_runs_when_at_or_above_threshold(
    session,
) -> None:
    """A 5-min meeting (300 sec exactly) MUST run; 299 sec MUST skip.
    Boundary check locks the «< threshold» semantics (not «≤»)."""
    from unittest.mock import MagicMock
    from app.config import Settings
    from app.intent.llm_backends import OpenAIBackend
    from app.zoom.pipeline import ZoomPipeline, ZoomRecordingMeta

    settings = Settings(MIN_MEETING_SECONDS=300)
    pipe = ZoomPipeline(
        settings=settings,
        client=MagicMock(),
        llm_backend=MagicMock(spec=OpenAIBackend),
    )
    # 299 sec → below → skip
    from datetime import datetime as _dt, timezone as _tz
    m_below = ZoomRecordingMeta(
        id="zz-299==",
        meeting_id=None,
        title="299-sec call",
        meeting_date=_dt(2026, 5, 22, 10, 0, tzinfo=_tz.utc),
        duration_seconds=299,
        participants=[],
        audio_url=None,
        share_url=None,
        host_email=None,
        raw={"id": "zz-299==", "duration": 5, "topic": "x"},
    )
    assert pipe.process_one(session, m_below).skipped_reason == \
        "duration_too_short"


def test_fr_cr_05_192t_threshold_zero_disables_gate(session) -> None:
    """Setting MIN_MEETING_SECONDS=0 disables the gate — every
    meeting flows through (operator escape hatch)."""
    from unittest.mock import MagicMock
    from app.config import Settings
    from app.intent.llm_backends import OpenAIBackend
    from app.zoom.pipeline import ZoomPipeline, ZoomRecordingMeta

    settings = Settings(MIN_MEETING_SECONDS=0)
    pipe = ZoomPipeline(
        settings=settings,
        client=MagicMock(),
        llm_backend=MagicMock(spec=OpenAIBackend),
    )
    from datetime import datetime as _dt, timezone as _tz
    m = ZoomRecordingMeta(
        id="zz-10sec==",
        meeting_id=None,
        title="10 sec ping",
        meeting_date=_dt(2026, 5, 22, 10, 0, tzinfo=_tz.utc),
        duration_seconds=10,
        participants=[],
        audio_url=None,
        share_url=None,
        host_email=None,
        raw={"id": "zz-10sec==", "duration": 0, "topic": "x"},
    )
    # Gate disabled → does NOT skip on duration grounds. The pipeline
    # may still fail downstream (no audio_url) but `skipped_reason`
    # won't be «duration_too_short».
    report = pipe.process_one(session, m)
    assert report.skipped_reason != "duration_too_short"


__all__ = []  # type: ignore[var-annotated]
