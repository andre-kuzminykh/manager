"""FR-CR-05-257 — Zoom process_one already-processed short-circuit.

Symmetric with Fireflies. A ZoomRecording whose whole pipeline is done
(processed_at + all six step flags) MUST be skipped with
`report.skipped_reason="already_processed"` BEFORE re-entering any step —
otherwise an out-of-band flag state (e.g. a non-null last_error left on a
fully-processed row, or republish --regenerate clearing short_summary_sent)
would re-fire the Slack mirror / n8n webhook / task cards on the next poll.

The guard fires AFTER `_upsert_recording` (row still loaded for audit) but
BEFORE the duration gate and any LLM step.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.config import Settings
from app.intent.llm_backends import OpenAIBackend
from app.models import ZoomRecording
from app.zoom.pipeline import ZoomPipeline, ZoomRecordingMeta


def _fully_processed_row(zoom_id: str) -> ZoomRecording:
    return ZoomRecording(
        zoom_id=zoom_id,
        title="Done meeting",
        meeting_date=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
        duration_seconds=3600,
        participants=[],
        processed_at=datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc),
        audio_downloaded=True,
        transcribed=True,
        detailed_summarised=True,
        doc_exported=True,
        short_summary_sent=True,
        tasks_extracted=True,
    )


def test_fully_processed_recording_is_skipped(session) -> None:
    session.add(_fully_processed_row("zz-done=="))
    session.flush()

    pipe = ZoomPipeline(
        settings=Settings(MIN_MEETING_SECONDS=300),
        client=MagicMock(),
        llm_backend=MagicMock(spec=OpenAIBackend),
    )
    m = ZoomRecordingMeta(
        id="zz-done==",
        meeting_id=None,
        title="Done meeting",
        meeting_date=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
        duration_seconds=3600,
        participants=[],
        audio_url=None,
        share_url=None,
        host_email=None,
        raw={"id": "zz-done==", "duration": 60, "topic": "Done meeting"},
    )
    report = pipe.process_one(session, m)
    assert report.skipped_reason == "already_processed"
    # No LLM step ran.
    pipe._llm.call_tool.assert_not_called()


def test_incomplete_recording_is_not_short_circuited(session) -> None:
    """A row missing one flag (short_summary_sent) must NOT be treated as
    already_processed — it should fall through past the guard."""
    row = _fully_processed_row("zz-partial==")
    row.short_summary_sent = False
    session.add(row)
    session.flush()

    pipe = ZoomPipeline(
        settings=Settings(MIN_MEETING_SECONDS=300),
        client=MagicMock(),
        llm_backend=MagicMock(spec=OpenAIBackend),
    )
    m = ZoomRecordingMeta(
        id="zz-partial==",
        meeting_id=None,
        title="Done meeting",
        meeting_date=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
        duration_seconds=3600,
        participants=[],
        audio_url=None,
        share_url=None,
        host_email=None,
        raw={"id": "zz-partial==", "duration": 60, "topic": "Done meeting"},
    )
    report = pipe.process_one(session, m)
    assert report.skipped_reason != "already_processed"
