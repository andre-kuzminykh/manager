"""FR-CR-05-209 — defer not-ready Fireflies transcripts (Kima Ventures
regression: a real meeting got a roster-only transcript → «Запись без
содержимого»). Pure readiness check + the process_one defer helper (via a
duck-typed stub — no DB / no network).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.fireflies.pipeline import FirefliesPipeline
from app.services.transcription import is_native_transcript_ready


# -- pure readiness check -----------------------------------------------------

def test_is_native_transcript_ready() -> None:
    assert is_native_transcript_ready("x" * 700) is True
    assert is_native_transcript_ready("x" * 600) is True       # inclusive
    assert is_native_transcript_ready("x" * 599) is False
    assert is_native_transcript_ready("") is False
    assert is_native_transcript_ready(None) is False
    assert is_native_transcript_ready("   \n  ") is False
    assert is_native_transcript_ready("short", min_chars=3) is True


# -- defer helper (FF process_one gate) ---------------------------------------

class _S:
    def __init__(self, prefer=True, grace=6.0, min_chars=600):
        self.fireflies_prefer_native_transcript = prefer
        self.fireflies_transcript_grace_hours = grace
        self.fireflies_transcript_min_ready_chars = min_chars


class _Client:
    def __init__(self, native="", raise_=False):
        self._native = native
        self._raise = raise_
    def fetch_transcript_text(self, fid):
        if self._raise:
            raise RuntimeError("ff down")
        return self._native


class _Pipe:
    def __init__(self, settings, client):
        self._settings = settings
        self._client = client


class _Row:
    def __init__(self, *, transcribed=False, transcript_text=None,
                 meeting_date=None, fireflies_id="f1"):
        self.transcribed = transcribed
        self.transcript_text = transcript_text
        self.meeting_date = meeting_date
        self.fireflies_id = fireflies_id


def _not_ready(settings, client, row) -> bool:
    return FirefliesPipeline._ff_native_transcript_not_ready(_Pipe(settings, client), row)


_RECENT = datetime.now(timezone.utc) - timedelta(minutes=10)
_OLD = datetime.now(timezone.utc) - timedelta(hours=10)


def test_recent_empty_native_defers() -> None:
    assert _not_ready(_S(), _Client(native=""), _Row(meeting_date=_RECENT)) is True


def test_recent_short_native_defers() -> None:
    assert _not_ready(_S(), _Client(native="Sam.\nHi.\n" * 5),
                      _Row(meeting_date=_RECENT)) is True


def test_recent_real_native_proceeds() -> None:
    assert _not_ready(_S(), _Client(native="x" * 700),
                      _Row(meeting_date=_RECENT)) is False


def test_already_transcribed_proceeds() -> None:
    assert _not_ready(_S(), _Client(native=""),
                      _Row(transcribed=True, transcript_text="real text",
                           meeting_date=_RECENT)) is False


def test_native_not_preferred_proceeds() -> None:
    assert _not_ready(_S(prefer=False), _Client(native=""),
                      _Row(meeting_date=_RECENT)) is False


def test_past_grace_window_proceeds() -> None:
    # genuinely-empty old meeting must NOT defer forever — content gate handles it
    assert _not_ready(_S(), _Client(native=""), _Row(meeting_date=_OLD)) is False


def test_fetch_error_proceeds() -> None:
    # best-effort: a fetch error never blocks the meeting
    assert _not_ready(_S(), _Client(raise_=True), _Row(meeting_date=_RECENT)) is False


def test_no_meeting_date_uses_native_only() -> None:
    # no date → skip the grace check, decide purely on native readiness
    assert _not_ready(_S(), _Client(native=""), _Row(meeting_date=None)) is True
    assert _not_ready(_S(), _Client(native="x" * 700), _Row(meeting_date=None)) is False


__all__: list[str] = []
