"""FR-CR-05-195 — ID-locked tests для Fireflies duration units fix.

Fireflies GraphQL `duration` поле возвращает minutes, не seconds.
До фикса все meeting'и сохранялись с маленьким duration_seconds и
ошибочно скипались через min_meeting_seconds=300 filter.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_fr_cr_05_195_duration_multiplied_by_60_at_ingestion() -> None:
    """Fireflies API возвращает `duration=58` (минуты для часовой встречи).
    Ingestion должен сохранить как `duration_seconds=3480` (минуты × 60)."""
    from app.fireflies.client import FirefliesClient
    fake_response = {
        "data": {
            "transcripts": [
                {
                    "id": "abc123",
                    "title": "Erik Goodman Interview",
                    "date": 1716301800000,  # millis
                    "duration": 58,  # 58 minutes
                    "participants": ["artem@thehumanoid.ai"],
                    "audio_url": "https://example/audio.mp3",
                    "transcript_url": "https://example/transcript",
                    "meeting_attendees": [],
                }
            ]
        }
    }
    client = FirefliesClient(api_key="test")
    client._request_func = lambda url, headers, body: fake_response
    rows = client.list_transcripts(limit=10)
    assert len(rows) == 1
    # 58 minutes × 60 = 3480 seconds
    assert rows[0].duration_seconds == 3480


def test_fr_cr_05_195_duration_zero_stays_zero() -> None:
    """Записи с duration=0 (audio_*.ogg без замера) должны остаться 0,
    не умножаться (0 × 60 = 0 правильно)."""
    from app.fireflies.client import FirefliesClient
    fake_response = {
        "data": {
            "transcripts": [
                {
                    "id": "abc",
                    "title": "audio.ogg",
                    "date": 1716301800000,
                    "duration": 0,
                    "participants": [],
                    "audio_url": None,
                    "transcript_url": None,
                    "meeting_attendees": [],
                }
            ]
        }
    }
    client = FirefliesClient(api_key="test")
    client._request_func = lambda url, headers, body: fake_response
    rows = client.list_transcripts(limit=10)
    assert rows[0].duration_seconds == 0


def test_fr_cr_05_195_duration_float_converted_to_int_seconds() -> None:
    """Fireflies может вернуть float (например 25.5 min). Ingestion
    должен сделать `int(25.5 * 60) = 1530` (не int(25.5)=25 потом ×60)."""
    from app.fireflies.client import FirefliesClient
    fake_response = {
        "data": {
            "transcripts": [
                {
                    "id": "abc",
                    "title": "x",
                    "date": 1716301800000,
                    "duration": 25.5,  # 25.5 minutes
                    "participants": [],
                    "audio_url": None,
                    "transcript_url": None,
                    "meeting_attendees": [],
                }
            ]
        }
    }
    client = FirefliesClient(api_key="test")
    client._request_func = lambda url, headers, body: fake_response
    rows = client.list_transcripts(limit=10)
    # 25.5 × 60 = 1530 seconds
    assert rows[0].duration_seconds == 1530


def test_fr_cr_05_195_duration_none_passthrough() -> None:
    """Если duration field отсутствует — duration_seconds=None."""
    from app.fireflies.client import FirefliesClient
    fake_response = {
        "data": {
            "transcripts": [
                {
                    "id": "abc",
                    "title": "x",
                    "date": 1716301800000,
                    # no `duration` field
                    "participants": [],
                    "audio_url": None,
                    "transcript_url": None,
                    "meeting_attendees": [],
                }
            ]
        }
    }
    client = FirefliesClient(api_key="test")
    client._request_func = lambda url, headers, body: fake_response
    rows = client.list_transcripts(limit=10)
    assert rows[0].duration_seconds is None
