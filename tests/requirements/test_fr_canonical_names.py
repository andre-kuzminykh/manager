"""FR-CR-05-251 — task canonicalization uses the FR/MCP canonical names for
the meeting (resolver output), not the stale local Counterparty directory.

Pure unit test of the `fr_canonical_names` helper that turns a meeting's
``_*_detail_canon_map`` ({mention: canonical}) into the task-canon vocabulary.
"""
from __future__ import annotations

from app.services.counterparty_match import fr_canonical_names


def test_empty_inputs() -> None:
    assert fr_canonical_names(None) == []
    assert fr_canonical_names({}) == []


def test_unique_canonical_values() -> None:
    m = {
        "Амазону": "Amazon Industrial Fund",
        "Инвос": "Invus",
        "Расклиф": "Rosecliff",
        "Аэроджемель": "Abdul Latif Jameel Ventures",
        "Артем": "Артем Соколов",
    }
    out = fr_canonical_names(m)
    # all canonical VALUES surface (not the Cyrillic mention keys)
    assert "Amazon Industrial Fund" in out
    assert "Invus" in out
    assert "Rosecliff" in out
    assert "Abdul Latif Jameel Ventures" in out
    # mention keys must NOT leak in
    assert "Амазону" not in out
    # the stale forms that the old directory injected are absent
    assert "Amazon.com" not in out
    assert "SDF" not in out


def test_case_insensitive_dedup_first_spelling_wins() -> None:
    m = {"a": "Invus", "b": "invus", "c": "INVUS"}
    out = fr_canonical_names(m)
    assert out == ["Invus"]  # one entry, first spelling kept


def test_blank_values_skipped() -> None:
    m = {"a": "Kima Ventures", "b": "   ", "c": "", "d": None}
    assert fr_canonical_names(m) == ["Kima Ventures"]


def test_sorted_case_insensitively() -> None:
    m = {"1": "zeta", "2": "Alpha", "3": "mid"}
    assert fr_canonical_names(m) == ["Alpha", "mid", "zeta"]


__all__: list[str] = []
