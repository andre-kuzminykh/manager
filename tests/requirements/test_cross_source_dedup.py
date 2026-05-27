"""FR-CR-05-203 — cross-source Zoom↔Fireflies meeting dedup.

A meeting recorded by BOTH Zoom Cloud AND the Fireflies bot produces two
independent rows; per-row idempotency (slack_post_ts) can't dedupe them.
`process_one` of each pipeline must skip the second capture entirely
(no download / transcribe / post) when the other source already posted
the same meeting.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock


# === normalize_meeting_title ===


def test_fr_cr_05_203_normalize_strips_date_prefix() -> None:
    """FF prepends «DD/MM - » to the title; zoom keeps it raw. They must
    normalize to the same key so the dedup matches them."""
    from app.services.meeting_dedup import normalize_meeting_title

    a = normalize_meeting_title("27/05 - Weekly Top Management meeting")
    b = normalize_meeting_title("Weekly Top Management meeting")
    assert a == b == "weekly top management meeting"
    # full date variant + extra whitespace
    assert normalize_meeting_title("27/05/2026 -  POC   Goals") == \
        normalize_meeting_title("POC Goals")


# === find_cross_source_duplicate (helper, no real DB) ===


class _Row:
    def __init__(self, id: str, title: str) -> None:
        self.id = id
        self.title = title


def test_fr_cr_05_203_helper_matches_other_source() -> None:
    """A posted FF row of the same meeting → returns ('fireflies', id)."""
    from app.services.meeting_dedup import find_cross_source_duplicate

    class FakeSession:
        def execute(self, q, params):
            if "meeting_recordings" in str(q):
                return [_Row("ff1", "27/05 - Weekly Top Mgmt")]
            return []  # zoom_recordings: nothing posted

    res = find_cross_source_duplicate(
        FakeSession(),
        title="Weekly Top Mgmt",
        meeting_date=datetime(2026, 5, 27, 10, 5, tzinfo=timezone.utc),
        self_kind="zoom",
        self_id="z1",
    )
    assert res == ("fireflies", "ff1")


def test_fr_cr_05_203_no_duplicate_proceeds() -> None:
    """Nothing posted for this meeting → None (pipeline proceeds)."""
    from app.services.meeting_dedup import find_cross_source_duplicate

    class FakeSession:
        def execute(self, q, params):
            return []

    res = find_cross_source_duplicate(
        FakeSession(),
        title="Some unique meeting",
        meeting_date=datetime(2026, 5, 27, 10, 0, tzinfo=timezone.utc),
        self_kind="zoom",
        self_id="z1",
    )
    assert res is None


def test_fr_cr_05_203_helper_excludes_self() -> None:
    """The row's own posted record must not count as a duplicate."""
    from app.services.meeting_dedup import find_cross_source_duplicate

    class FakeSession:
        def execute(self, q, params):
            if "zoom_recordings" in str(q):
                return [_Row("z1", "Weekly Top Mgmt")]  # this is self
            return []

    res = find_cross_source_duplicate(
        FakeSession(),
        title="Weekly Top Mgmt",
        meeting_date=datetime(2026, 5, 27, 10, 0, tzinfo=timezone.utc),
        self_kind="zoom",
        self_id="z1",
    )
    assert res is None


def test_fr_cr_05_203_helper_defensive_on_db_error() -> None:
    """Any DB error → None (dedup must never block processing)."""
    from app.services.meeting_dedup import find_cross_source_duplicate

    class BoomSession:
        def execute(self, q, params):
            raise RuntimeError("db down")

    res = find_cross_source_duplicate(
        BoomSession(),
        title="X meeting",
        meeting_date=datetime(2026, 5, 27, 10, 0, tzinfo=timezone.utc),
        self_kind="zoom",
        self_id="z1",
    )
    assert res is None


# === process_one integration: skip the second capture ===


def test_fr_cr_05_203_zoom_skips_when_other_source_posted(monkeypatch) -> None:
    """Zoom process_one: if FF already posted this meeting → skip BEFORE
    download (skipped_reason='duplicate_other_source')."""
    from app.zoom.pipeline import ZoomPipeline
    from app.models import ZoomRecording
    from app.services import meeting_dedup

    row = MagicMock(spec=ZoomRecording)
    row.zoom_id = "zdup"
    row.attempts = 0
    row.duration_seconds = 1800
    row.audio_url = "http://x/audio.m4a"
    row.audio_downloaded = False
    row.audio_path = None
    row.last_error = None
    row.title = "Weekly Top Management meeting"
    row.meeting_date = datetime(2026, 5, 27, 10, 8, tzinfo=timezone.utc)

    pipeline = ZoomPipeline.__new__(ZoomPipeline)
    pipeline._settings = MagicMock(min_meeting_seconds=300)
    pipeline._upsert_recording = lambda session, m: row
    pipeline._step_download_audio = MagicMock()  # must NOT be called

    monkeypatch.setattr(
        meeting_dedup, "find_cross_source_duplicate",
        lambda *a, **k: ("fireflies", "ff1"),
    )

    report = pipeline.process_one(MagicMock(), MagicMock())
    assert report.skipped_reason == "duplicate_other_source"
    pipeline._step_download_audio.assert_not_called()


def test_fr_cr_05_203_fireflies_symmetric(monkeypatch) -> None:
    """Fireflies process_one: if Zoom already posted this meeting → skip
    BEFORE download (symmetric to Zoom)."""
    from app.fireflies.pipeline import FirefliesPipeline
    from app.models import MeetingRecording
    from app.services import meeting_dedup

    row = MagicMock(spec=MeetingRecording)
    row.fireflies_id = "ffdup"
    row.attempts = 0
    row.duration_seconds = 1800
    row.audio_url = "http://x/audio.mp3"
    row.audio_downloaded = False
    row.audio_path = None
    row.processed_at = None
    row.transcribed = False
    row.detailed_summarised = False
    row.doc_exported = False
    row.short_summary_sent = False
    row.tasks_extracted = False
    row.last_error = None
    row.title = "27/05 - POC Goals"
    row.meeting_date = datetime(2026, 5, 27, 10, 10, tzinfo=timezone.utc)

    pipeline = FirefliesPipeline.__new__(FirefliesPipeline)
    pipeline._settings = MagicMock(min_meeting_seconds=300)
    pipeline._upsert_recording = lambda session, t: row
    pipeline._step_download_audio = MagicMock()  # must NOT be called

    monkeypatch.setattr(
        meeting_dedup, "find_cross_source_duplicate",
        lambda *a, **k: ("zoom", "z1"),
    )

    report = pipeline.process_one(MagicMock(), MagicMock())
    assert report.skipped_reason == "duplicate_other_source"
    pipeline._step_download_audio.assert_not_called()
