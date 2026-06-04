"""FR-CR-05-256 — canonicalize_text must do ONE non-overlapping pass so a
rewrite whose VALUE contains another mention key cannot cascade
(«Vinrobotics» → «Vinrobotics/ Vinventures» → «Vinrobotics/ Vinrobotics/ …»).
Pure tests of the string substitution — no DB / no LLM.
"""
from __future__ import annotations

from app.services.counterparty_match import canonicalize_text as ct


def test_value_contains_another_key_no_cascade() -> None:
    m = {"Vinrobotics": "Vinrobotics/ Vinventures",
         "Vinventures": "Vinrobotics/ Vinventures"}
    out = ct("met Vinrobotics and Vinventures", m)
    # each surface replaced exactly once — no «Vinrobotics/ Vinrobotics/ …»
    assert out == "met Vinrobotics/ Vinventures and Vinrobotics/ Vinventures"
    assert "Vinrobotics/ Vinrobotics" not in out
    assert ct("Vinrobotics", m) == "Vinrobotics/ Vinventures"


def test_prefix_guard_preserved() -> None:
    m = {"Insight": "Insight Partners"}
    assert ct("Insight today", m) == "Insight Partners today"
    # already canonical → don't append « Partners» twice
    assert ct("Insight Partners today", m) == "Insight Partners today"


def test_longest_mention_wins() -> None:
    m = {"Bauer": "BAUER", "Bauer/Dart": "Bauer Dart Group"}
    assert ct("Bauer/Dart deal", m) == "Bauer Dart Group deal"


def test_cross_lingual_case_form() -> None:
    assert ct("Амазону письмо", {"Амазону": "Amazon Industrial Fund"}) \
        == "Amazon Industrial Fund письмо"


def test_word_boundary_no_partial() -> None:
    # «Sot» must not eat «Sotirios»
    assert ct("Sotirios came", {"Sot": "Sotirios Stasinopoulos"}) == "Sotirios came"


def test_noops() -> None:
    assert ct(None, {"a": "b"}) is None
    assert ct("text", {}) == "text"
    assert ct("text", {"x": "x"}) == "text"   # identity skipped


__all__: list[str] = []
