"""FR-CR-05-224/228 — review harness pure helpers.

The harness is an operator-run read-only tool (needs DB + OpenAI),
verified by a live trace on Zoom + Fireflies. Here we pin the two pure
helpers: `_context_for` (snippet around a mention) and
`_summary_hit_count` (how many times each matched-entity name appears
in the saved detailed_summary — the «попало ли в финальный artefact»
signal the trace prints).
"""
from __future__ import annotations

from ops.review_entity_match import _context_for, _summary_hit_count


def test_context_window_centers_on_mention():
    transcript = "x" * 1000 + "ADNOC discussion here" + "y" * 1000
    ctx = _context_for(transcript, "ADNOC", window=50)
    assert "ADNOC" in ctx
    assert len(ctx) <= 50 + len("ADNOC") + 50


def test_context_window_case_insensitive():
    transcript = "before нефтянка Адног потом после"
    ctx = _context_for(transcript, "адног", window=10)
    assert "Адног" in ctx


def test_context_window_falls_back_to_head_when_absent():
    transcript = "no mention of the target here at all"
    ctx = _context_for(transcript, "ZZZ", window=12)
    assert ctx == transcript[:12]


def test_summary_hit_count_counts_case_insensitive():
    summary = "Today we met with ADNOC and discussed adnoc plans. Also Tether."
    hits = _summary_hit_count(summary, ["ADNOC", "Tether", "Goldman"])
    assert hits["ADNOC"] == 2  # ADNOC + adnoc
    assert hits["Tether"] == 1
    assert hits["Goldman"] == 0


def test_summary_hit_count_safe_on_none():
    assert _summary_hit_count(None, ["X"]) == {}


def test_summary_hit_count_skips_blanks():
    hits = _summary_hit_count("plain summary", ["", None, "summary"])  # type: ignore[list-item]
    assert hits == {"summary": 1}
