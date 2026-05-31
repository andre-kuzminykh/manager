"""FR-CR-05-224 — review harness: pure helper coverage.

The harness itself is an operator-run, read-only review tool (needs DB +
OpenAI), verified by a live run on the isolated pgvector test DB. Here we
pin the one pure helper — the context-window extractor that gives the
critic local transcript context around each mention.
"""
from __future__ import annotations

from ops.review_entity_match import _context_for


def test_context_window_centers_on_mention():
    transcript = "x" * 1000 + "ADNOC discussion here" + "y" * 1000
    ctx = _context_for(transcript, "ADNOC", window=50)
    assert "ADNOC" in ctx
    # window is bounded, not the whole transcript
    assert len(ctx) <= 50 + len("ADNOC") + 50


def test_context_window_case_insensitive():
    transcript = "before нефтянка Адног потом после"
    ctx = _context_for(transcript, "адног", window=10)
    assert "Адног" in ctx


def test_context_window_falls_back_to_head_when_absent():
    transcript = "no mention of the target here at all"
    ctx = _context_for(transcript, "ZZZ", window=12)
    assert ctx == transcript[:12]
