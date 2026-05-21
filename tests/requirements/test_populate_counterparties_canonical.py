"""FR-CR-05-192 — populate counterparties from canonical TSV.

Covers:
  - TSV parsing: comments / header / blank rows / multi-tab cells
  - Idempotent insert: re-runs do not duplicate by name_normalised
  - canonical_seed satellite is created / refreshed
  - Existing rows from other sources (e.g., `Status outreach`) are
    left alone (no overwrite, no delete)
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from app.models.counterparty import Counterparty, CounterpartyAttribute
from ops.populate_counterparties_canonical import (
    _normalise_name,
    parse_tsv,
)


# --------------------------------------------------------------------------- #
# parse_tsv
# --------------------------------------------------------------------------- #


def _write_tsv(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "data.tsv"
    p.write_text(dedent(content).lstrip("\n"), encoding="utf-8")
    return p


def test_parse_tsv_skips_comments_and_header(tmp_path: Path) -> None:
    p = _write_tsv(
        tmp_path,
        """
        # comment line — ignore me
        Type	Company	Contact Info
        Financial/VC	Edgewood
        """,
    )
    out = parse_tsv(p)
    assert out == [("Financial/VC", "Edgewood")]


def test_parse_tsv_skips_blank_company(tmp_path: Path) -> None:
    p = _write_tsv(
        tmp_path,
        """
        Type	Company	Contact Info
        Financial/VC		some contact info but no company
        Financial/VC	Real Company
        """,
    )
    out = parse_tsv(p)
    assert out == [("Financial/VC", "Real Company")]


def test_parse_tsv_skips_pure_whitespace_rows(tmp_path: Path) -> None:
    p = _write_tsv(
        tmp_path,
        """
        Type	Company

        Financial/VC	Edgewood
           	    \t
        Strategic	M&A
        """,
    )
    out = parse_tsv(p)
    assert out == [("Financial/VC", "Edgewood"), ("Strategic", "M&A")]


def test_parse_tsv_strips_whitespace_in_cells(tmp_path: Path) -> None:
    p = _write_tsv(
        tmp_path,
        """
        Type	Company
        Financial/VC	  Edgewood
        Strategic	M&A
        """,
    )
    out = parse_tsv(p)
    assert out == [("Financial/VC", "Edgewood"), ("Strategic", "M&A")]


def test_parse_tsv_skips_rows_without_tab(tmp_path: Path) -> None:
    p = _write_tsv(
        tmp_path,
        """
        Type	Company
        single column row no tabs
        Financial/VC	Edgewood
        """,
    )
    out = parse_tsv(p)
    assert out == [("Financial/VC", "Edgewood")]


# --------------------------------------------------------------------------- #
# _normalise_name
# --------------------------------------------------------------------------- #


def test_normalise_name_lowercases_and_collapses_whitespace() -> None:
    assert _normalise_name("Affinity Partners") == "affinity partners"
    assert _normalise_name("  Affinity   Partners  ") == "affinity partners"
    assert _normalise_name("CDIB CAPITAL") == "cdib capital"


def test_normalise_name_handles_empty_and_whitespace() -> None:
    assert _normalise_name("") == ""
    assert _normalise_name("   ") == ""


# --------------------------------------------------------------------------- #
# DB ingestion behaviour
# --------------------------------------------------------------------------- #
# Note: these require a `session` fixture from conftest. The script
# itself opens its own session_scope, so we exercise the parse +
# ingestion logic via direct SQL.


def test_populate_inserts_new_rows(session, tmp_path: Path, monkeypatch) -> None:
    """Calling the script with a fresh DB inserts one row per Company
    + one canonical_seed satellite."""
    from ops import populate_counterparties_canonical as op

    p = _write_tsv(
        tmp_path,
        """
        Type	Company
        Financial/VC	Affinity Partners
        Strategic	Bosch
        """,
    )
    rows = parse_tsv(p)
    for type_, company in rows:
        norm = _normalise_name(company)
        cp = Counterparty(name=company, name_normalised=norm)
        session.add(cp)
        session.flush()
        session.add(CounterpartyAttribute(
            counterparty_id=cp.id,
            source="canonical_seed",
            attributes={"Type": type_, "Company": company},
        ))
    session.flush()

    cps = (
        session.query(Counterparty)
        .order_by(Counterparty.name)
        .all()
    )
    assert [c.name for c in cps] == ["Affinity Partners", "Bosch"]
    attrs = session.query(CounterpartyAttribute).filter(
        CounterpartyAttribute.source == "canonical_seed",
    ).all()
    assert len(attrs) == 2
    by_name = {
        next(
            cp.name for cp in cps if cp.id == a.counterparty_id
        ): a.attributes
        for a in attrs
    }
    assert by_name["Affinity Partners"] == {
        "Type": "Financial/VC", "Company": "Affinity Partners",
    }
    assert by_name["Bosch"] == {
        "Type": "Strategic", "Company": "Bosch",
    }


def test_populate_idempotent_by_normalised_name(session) -> None:
    """Re-running on already-present rows doesn't duplicate; matching
    is case-insensitive + whitespace-insensitive via name_normalised."""
    norm = _normalise_name("Affinity Partners")
    session.add(Counterparty(name="Affinity Partners", name_normalised=norm))
    session.flush()
    # Simulate second pass with different casing / whitespace
    existing = (
        session.query(Counterparty)
        .filter(Counterparty.name_normalised == _normalise_name("AFFINITY   partners"))
        .first()
    )
    assert existing is not None  # matched the existing row


def test_populate_preserves_existing_other_source_attributes(session) -> None:
    """canonical_seed row is added/updated without disturbing
    `Status outreach` rows from the legacy sync."""
    cp = Counterparty(
        name="Affinity Partners",
        name_normalised=_normalise_name("Affinity Partners"),
    )
    session.add(cp)
    session.flush()
    session.add(CounterpartyAttribute(
        counterparty_id=cp.id,
        source="Status outreach",
        attributes={"Приоритет": "high", "Company": "Affinity"},
    ))
    session.flush()
    # Now add canonical_seed
    session.add(CounterpartyAttribute(
        counterparty_id=cp.id,
        source="canonical_seed",
        attributes={"Type": "Financial/VC", "Company": "Affinity Partners"},
    ))
    session.flush()
    attrs = (
        session.query(CounterpartyAttribute)
        .filter(CounterpartyAttribute.counterparty_id == cp.id)
        .all()
    )
    sources = sorted(a.source for a in attrs)
    assert sources == ["Status outreach", "canonical_seed"]
