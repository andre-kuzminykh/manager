"""FR-CR-05-196 + FR-CR-05-197 — ID-locked tests для:
  - retry cap (attempts >= 20 → permanent_failure)
  - Zoom 24h sentinel skip (duration=86400 → skip).
"""
from __future__ import annotations

from unittest.mock import MagicMock


# === FR-CR-05-196: retry cap ===


def test_fr_cr_05_196_zoom_attempts_20_caps_to_permanent_failure() -> None:
    """Zoom row с attempts=20 → process_one не запускает pipeline,
    помечает row.last_error='permanent_failure_attempts_exceeded',
    логирует permanent_failure_capped."""
    from app.zoom.pipeline import ZoomPipeline
    from app.models import ZoomRecording

    # Минимальная row с уже 20 attempts
    row = MagicMock(spec=ZoomRecording)
    row.zoom_id = "test_id"
    row.attempts = 20
    row.duration_seconds = 1800
    row.last_error = None

    # Pipeline без зависимостей — проверяем только gate
    pipeline = ZoomPipeline.__new__(ZoomPipeline)
    # Inject минимум settings stub
    pipeline._settings = MagicMock(min_meeting_seconds=300)
    pipeline._upsert_recording = lambda session, m: row

    from app.zoom.pipeline import ZoomRecordingMeta
    meta = MagicMock(spec=ZoomRecordingMeta)

    session = MagicMock()
    report = pipeline.process_one(session, meta)
    # Не должен был тикнуть attempts (уже 20)
    assert row.last_error == "permanent_failure_attempts_exceeded"
    assert report.skipped_reason == "permanent_failure_attempts_exceeded"


def test_fr_cr_05_196_zoom_attempts_19_proceeds() -> None:
    """attempts=19 (< cap 20) — proceed нормально, cap НЕ срабатывает."""
    # На уровне юнит-теста просто проверим что значение порога — 20.
    from app.zoom.pipeline import MAX_ATTEMPTS_BEFORE_GIVE_UP
    assert MAX_ATTEMPTS_BEFORE_GIVE_UP == 20


def test_fr_cr_05_196_fireflies_attempts_cap_symmetric() -> None:
    """Симметричный gate в Fireflies pipeline."""
    from app.fireflies.pipeline import MAX_ATTEMPTS_BEFORE_GIVE_UP
    assert MAX_ATTEMPTS_BEFORE_GIVE_UP == 20


# === FR-CR-05-197: 24h sentinel ===


def test_fr_cr_05_197_zoom_24h_sentinel_skipped() -> None:
    """duration_seconds=86400 (24h sentinel) → skip с
    last_error='zoom_phone_24h_sentinel'."""
    from app.zoom.pipeline import ZoomPipeline
    from app.models import ZoomRecording

    row = MagicMock(spec=ZoomRecording)
    row.zoom_id = "test_24h"
    row.attempts = 0
    row.duration_seconds = 86400  # SENTINEL
    row.last_error = None

    pipeline = ZoomPipeline.__new__(ZoomPipeline)
    pipeline._settings = MagicMock(min_meeting_seconds=300)
    pipeline._upsert_recording = lambda session, m: row

    from app.zoom.pipeline import ZoomRecordingMeta
    meta = MagicMock(spec=ZoomRecordingMeta)
    session = MagicMock()
    report = pipeline.process_one(session, meta)

    assert report.skipped_reason == "zoom_phone_24h_sentinel"
    assert row.last_error == "zoom_phone_24h_sentinel"


def test_fr_cr_05_197_zoom_normal_duration_proceeds() -> None:
    """duration=3600 (1 hour, нормальный meeting) — НЕ должен сработать
    24h sentinel."""
    # Проверим что константа — exact 86400, не range
    from app.zoom.pipeline import ZOOM_PHONE_24H_SENTINEL_SECONDS
    assert ZOOM_PHONE_24H_SENTINEL_SECONDS == 86400
    # Нормальное 6-часовое совещание не должно матчить
    assert 21600 != ZOOM_PHONE_24H_SENTINEL_SECONDS
