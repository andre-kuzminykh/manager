"""FR-NT-TR (SPEC_NATIVE_TRANSCRIPT_v0.1 §6) — pure-function gate that
decides native-transcript vs Whisper. No I/O; covers every branch of the
safety invariants (I1 flag-off identical, I2 fallback on empty/short).
"""
from __future__ import annotations

from app.services.transcription import should_use_native

_LONG = "x" * 150  # comfortably above the default 100-char floor


def test_prefer_with_long_native_uses_native() -> None:
    assert should_use_native(prefer_native=True, native_text=_LONG) is True


def test_flag_off_never_uses_native_even_if_long() -> None:
    # I1 — flag off ⇒ Whisper path, byte-identical to prior behaviour.
    assert should_use_native(prefer_native=False, native_text=_LONG) is False


def test_empty_native_falls_back_to_whisper() -> None:
    # I2 — empty / whitespace / None ⇒ Whisper.
    assert should_use_native(prefer_native=True, native_text="") is False
    assert should_use_native(prefer_native=True, native_text="   \n  ") is False
    assert should_use_native(prefer_native=True, native_text=None) is False


def test_short_native_falls_back_to_whisper() -> None:
    # A 2-line VTT stub is a fragment ⇒ Whisper.
    assert should_use_native(prefer_native=True, native_text="hi there") is False


def test_threshold_is_inclusive_and_configurable() -> None:
    exactly_100 = "y" * 100
    assert should_use_native(prefer_native=True, native_text=exactly_100) is True
    # Custom (higher) floor rejects the same text.
    assert (
        should_use_native(
            prefer_native=True, native_text=exactly_100, min_native_chars=101
        )
        is False
    )
    # Custom (lower) floor accepts short text.
    assert (
        should_use_native(
            prefer_native=True, native_text="short", min_native_chars=3
        )
        is True
    )


def test_strips_before_measuring() -> None:
    # Leading/trailing whitespace doesn't count toward the length floor.
    padded = "   " + ("z" * 50) + "   "          # 50 real chars < 100
    assert should_use_native(prefer_native=True, native_text=padded) is False


__all__: list[str] = []
