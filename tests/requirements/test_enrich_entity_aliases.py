"""FR-CR-05-238 — alias-union contract for the abbreviation enricher.

The enricher only ever ADDS aliases (recall booster for vector search);
it must never drop existing surface forms (those came from the dedup
merges) nor duplicate / echo the canonical name.
"""
from __future__ import annotations

from ops.enrich_entity_aliases import _alias_union


def test_union_appends_and_dedupes_case_insensitive():
    out = _alias_union("BG, Ballie Gifford", ["bg", "Baillie Gifford & Co"],
                       name="Baillie Gifford")
    assert out == "BG, Ballie Gifford, Baillie Gifford & Co"


def test_union_drops_alias_equal_to_name():
    out = _alias_union(None, ["Goldman Sachs Private Wealth Management", "GS PWM", "GSPWM"],
                       name="Goldman Sachs Private Wealth Management")
    assert out == "GS PWM, GSPWM"


def test_union_keeps_existing_when_no_additions():
    assert _alias_union("HKIC", [], name="Hong Kong Investment Corporation") == "HKIC"
    assert _alias_union(None, [], name="X") is None


def test_union_preserves_existing_order_existing_first():
    out = _alias_union("Хёндай", ["Hyundai Motor", "Хёндай"], name="Hyundai")
    assert out == "Хёндай, Hyundai Motor"  # dup Хёндай dropped, order stable


def test_union_strips_blanks():
    out = _alias_union("  ", ["  ", "a16z", ""], name="Andreessen Horowitz")
    assert out == "a16z"
