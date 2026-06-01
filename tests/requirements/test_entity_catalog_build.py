"""FR-CR-05-231 — deduplicated entity-catalog builder.

Covers the deterministic chunker, the strict-JSON extractor's parsing /
sanitising, the dedup-merge upsert, and the resumable orchestration —
all without touching the network or a real LLM.
"""
from __future__ import annotations

import textwrap

import pytest

from app.services import entity_catalog as ec


# ---------------------------------------------------------------------------
# Chunker
# ---------------------------------------------------------------------------
def _write(tmp_path, rows: list[str]) -> str:
    p = tmp_path / "src.txt"
    p.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return str(p)


def test_chunker_is_deterministic_and_covers_all_rows(tmp_path):
    rows = [f"Org{i}\tFinancial/VC\tdesc number {i}" for i in range(50)]
    path = _write(tmp_path, rows)

    a = list(ec.iter_source_chunks(path, max_chars=200))
    b = list(ec.iter_source_chunks(path, max_chars=200))
    # deterministic
    assert [(c.chunk_no, c.row_lo, c.row_hi) for c in a] == [
        (c.chunk_no, c.row_lo, c.row_hi) for c in b
    ]
    # contiguous, gap-free coverage starting at 0
    assert a[0].row_lo == 0
    for prev, nxt in zip(a, a[1:]):
        assert nxt.row_lo == prev.row_hi + 1
        assert nxt.chunk_no == prev.chunk_no + 1


def test_chunker_respects_char_budget(tmp_path):
    rows = [f"Org{i}\tdesc {i}" for i in range(40)]
    path = _write(tmp_path, rows)
    chunks = list(ec.iter_source_chunks(path, max_chars=120))
    assert len(chunks) > 1
    # every multi-row chunk stays within budget (single oversized rows are
    # allowed through unsplit)
    for c in chunks:
        if c.row_hi > c.row_lo:
            assert len(c.text) <= 120


def test_chunker_flattens_quoted_multiline_cells(tmp_path):
    # A quoted TSV cell spanning physical lines = ONE logical row.
    content = 'Schwarz\tFinancial/VC\t"Andreas Strähle\nJulian Marx"\n'
    p = tmp_path / "q.txt"
    p.write_text(content, encoding="utf-8")
    chunks = list(ec.iter_source_chunks(str(p), max_chars=9999))
    assert len(chunks) == 1
    assert "\n" not in chunks[0].text  # newline flattened to ' / '
    assert "Andreas Strähle / Julian Marx" in chunks[0].text


# ---------------------------------------------------------------------------
# Extractor parsing
# ---------------------------------------------------------------------------
class _StubBackend:
    def __init__(self, payload: str):
        self.payload = payload
        self.calls = 0

    def complete_text(self, **kwargs) -> str:  # noqa: D401
        self.calls += 1
        return self.payload


def test_extract_parses_and_sanitises():
    payload = (
        '{"entities": ['
        '{"name": "Acme Capital", "is_org": true, "parent_org": "ignored", "description": "VC"},'
        '{"name": "Jane Doe", "is_org": false, "parent_org": "Acme Capital", "description": "Partner"},'
        '{"name": "x", "is_org": true, "description": "too short"},'
        '{"name": "No Flag", "description": "missing is_org"}'
        ']}'
    )
    out = ec.extract_entities_from_chunk(
        "whatever", llm_backend=_StubBackend(payload), model="m"
    )
    assert len(out) == 2
    org, person = out
    assert org["name"] == "Acme Capital" and org["is_org"] is True
    assert org["parent_org"] is None  # orgs never carry parent_org
    assert person["is_org"] is False and person["parent_org"] == "Acme Capital"


def test_extract_tolerates_code_fences_and_bad_json():
    fenced = '```json\n{"entities": [{"name": "Foo Inc", "is_org": true, "description": "d"}]}\n```'
    out = ec.extract_entities_from_chunk(
        "x", llm_backend=_StubBackend(fenced), model="m"
    )
    assert [e["name"] for e in out] == ["Foo Inc"]

    out2 = ec.extract_entities_from_chunk(
        "x", llm_backend=_StubBackend("not json at all"), model="m"
    )
    assert out2 == []


def test_coerce_bool_accepts_string_forms():
    assert ec._coerce_bool("org") is True
    assert ec._coerce_bool("person") is False
    assert ec._coerce_bool("maybe") is None


# ---------------------------------------------------------------------------
# Dedup-merge upsert
# ---------------------------------------------------------------------------
def test_upsert_dedups_and_accumulates_context(session):
    from app.models.entity_catalog import EntityCatalogStaging

    row1, created1 = ec.upsert_entity(
        session,
        {"name": "Acme Capital", "is_org": True, "parent_org": None,
         "description": "A VC fund."},
    )
    assert created1 is True

    # Same entity (legal suffix dropped by normalise_name → identical key),
    # fuller surface form + new context → merge, not duplicate.
    row2, created2 = ec.upsert_entity(
        session,
        {"name": "Acme Capital Ltd", "is_org": True, "parent_org": None,
         "description": "Invests in robotics."},
    )
    assert created2 is False
    assert row1.id == row2.id
    assert session.query(EntityCatalogStaging).count() == 1
    assert row2.name == "Acme Capital Ltd"  # longer surface promoted
    assert "Acme Capital" in (row2.aliases or "")  # shorter kept as alias
    assert "A VC fund." in row2.description and "robotics" in row2.description
    assert row2.mentions_count == 2


def test_upsert_keeps_org_and_person_separate(session):
    from app.models.entity_catalog import EntityCatalogStaging

    ec.upsert_entity(
        session, {"name": "Dyson", "is_org": True, "parent_org": None, "description": "Co"}
    )
    ec.upsert_entity(
        session,
        {"name": "Dyson", "is_org": False, "parent_org": "Dyson Ltd", "description": "Founder"},
    )
    assert session.query(EntityCatalogStaging).count() == 2


def test_upsert_fills_missing_parent_org(session):
    row, _ = ec.upsert_entity(
        session, {"name": "Jane Roe", "is_org": False, "parent_org": None, "description": "x"}
    )
    assert row.parent_org is None
    row2, _ = ec.upsert_entity(
        session,
        {"name": "Jane Roe", "is_org": False, "parent_org": "Foo Fund", "description": "y"},
    )
    assert row2.parent_org == "Foo Fund"


def test_merge_description_no_dupe_when_contained():
    assert ec.merge_description("hello world", "hello") == "hello world"
    assert ec.merge_description("", "fresh") == "fresh"
    merged = ec.merge_description("alpha", "beta")
    assert "alpha" in merged and "beta" in merged


# ---------------------------------------------------------------------------
# Orchestration + checkpoints
# ---------------------------------------------------------------------------
def test_ingest_is_resumable_and_idempotent(session, tmp_path, monkeypatch):
    from app.models.entity_catalog import EntityCatalogIngest

    rows = [f"Org{i}\tFinancial/VC\tdesc {i}" for i in range(12)]
    path = _write(tmp_path, rows)

    # one fresh entity per chunk
    counter = {"n": 0}

    def fake_extract(text, **kwargs):
        counter["n"] += 1
        return [{"name": f"Entity {counter['n']}", "is_org": True,
                 "parent_org": None, "description": text[:20]}]

    monkeypatch.setattr(ec, "extract_entities_from_chunk", fake_extract)

    stats1 = ec.ingest_source(
        session, path=path, llm_backend=object(), model="m",
        max_chars=60, limit_chunks=2, commit=session.flush,
    )
    assert stats1["chunks_processed"] == 2
    assert session.query(EntityCatalogIngest).count() == 2
    calls_after_first = counter["n"]

    # Resume: the 2 done chunks are skipped, only NEW chunks call the LLM.
    stats2 = ec.ingest_source(
        session, path=path, llm_backend=object(), model="m",
        max_chars=60, limit_chunks=2, commit=session.flush,
    )
    assert stats2["chunks_skipped"] >= 2
    assert stats2["chunks_processed"] == 2
    assert counter["n"] == calls_after_first + 2  # no repeat calls on done chunks


# ---------------------------------------------------------------------------
# Near-duplicate audit
# ---------------------------------------------------------------------------
def _it(id_, name, norm, is_org):
    return {"id": id_, "name": name, "name_normalised": norm, "is_org": is_org}


def test_find_dupes_flags_containment_and_close_ratio():
    items = [
        _it(1, "Sebastian Thrun", "sebastian thrun", False),
        _it(2, "Sebastian Thrun Ph.D", "sebastian thrun ph.d", False),  # contains #1
        _it(3, "Mirae", "mirae", True),
        _it(4, "Mirae Asset", "mirae asset", True),  # contains #3
        _it(5, "Goldman Sachs", "goldman sachs", True),  # unique
    ]
    clusters = ec.find_duplicate_candidates(items)
    flat = {it["id"] for c in clusters for it in c}
    assert {1, 2} <= flat and {3, 4} <= flat  # both near-dup pairs surfaced
    assert 5 not in flat  # the unique org is not in any cluster


def test_find_dupes_keeps_org_and_person_apart():
    # Same normalised string but different kind must NOT be merged into a
    # cluster (a company and its eponymous founder are distinct entities).
    items = [
        _it(1, "Dyson", "dyson", True),
        _it(2, "Dyson", "dyson", False),
    ]
    assert ec.find_duplicate_candidates(items) == []


def test_find_dupes_unions_into_clusters():
    items = [
        _it(1, "Acme Capital", "acme capital", True),
        _it(2, "Acme Capital Partners", "acme capital partners", True),
        _it(3, "Acme Capital Partners LP", "acme capital partners lp", True),
    ]
    clusters = ec.find_duplicate_candidates(items)
    assert len(clusters) == 1
    assert {it["id"] for it in clusters[0]} == {1, 2, 3}


def test_find_dupes_ignores_generic_token_collisions():
    # Orgs that share ONLY a generic word ('capital') must not cluster.
    items = [
        _it(1, "Vest Capital", "vest capital", True),
        _it(2, "IST Capital", "ist capital", True),
        _it(3, "JAM Capital Partners", "jam capital partners", True),
        _it(4, "D1 Capital Partners", "d1 capital partners", True),
    ]
    assert ec.find_duplicate_candidates(items) == []


def test_find_dupes_legal_suffix_still_merges():
    # A fuller legal / descriptive name (NOT an arm marker) still clusters.
    items = [
        _it(1, "Phoenix Court", "phoenix court", True),
        _it(2, "Phoenix Court Group Limited", "phoenix court group limited", True),
        _it(3, "Acme", "acme", True),
        _it(4, "Acme Ltd", "acme ltd", True),
    ]
    clusters = ec.find_duplicate_candidates(items)
    pairs = {frozenset(it["id"] for it in c) for c in clusters}
    assert frozenset({1, 2}) in pairs and frozenset({3, 4}) in pairs


def test_find_dupes_investment_arm_kept_separate():
    # operator: «о инвест-арм сохраняй» — parent vs investment vehicle that
    # differ ONLY by an arm marker must NOT be clustered.
    items = [
        _it(1, "Anthropic", "anthropic", True),
        _it(2, "Anthropic Capital", "anthropic capital", True),
        _it(3, "Accenture", "accenture", True),
        _it(4, "Accenture Ventures", "accenture ventures", True),
        _it(5, "Salesforce", "salesforce", True),
        _it(6, "Salesforce Ventures", "salesforce ventures", True),
        # arm with a descriptive name (marker not the only extra token)
        _it(7, "Amazon", "amazon", True),
        _it(8, "Amazon Industrial Innovation Fund",
            "amazon industrial innovation fund", True),
    ]
    assert ec.find_duplicate_candidates(items) == []


def test_find_dupes_bare_first_name_not_clustered():
    items = [
        _it(1, "Andrew", "andrew", False),
        _it(2, "Andrew Kang", "andrew kang", False),
        _it(3, "Andrew Wooten", "andrew wooten", False),
    ]
    assert ec.find_duplicate_candidates(items) == []


def test_find_dupes_typo_person_clustered():
    items = [
        _it(1, "Artem Tokarenko", "artem tokarenko", False),
        _it(2, "Artem Tikarenko", "artem tikarenko", False),
    ]
    clusters = ec.find_duplicate_candidates(items)
    assert len(clusters) == 1 and {it["id"] for it in clusters[0]} == {1, 2}


def test_find_dupes_descriptor_does_not_chain_unrelated_brands():
    """FR-CR-05-231 (live audit) — short brands sharing only «investment(s)»
    must NOT chain into one mega-cluster. Same-brand variants still merge."""
    items = [
        _it(1, "SBI", "sbi", True),
        _it(2, "SBI Investment", "sbi investment", True),
        _it(3, "SBI Investments", "sbi investments", True),
        _it(4, "KB Investment", "kb investment", True),
        _it(5, "LR Investments", "lr investments", True),
        _it(6, "ARK Investment", "ark investment", True),
        _it(7, "ARK Investment Management", "ark investment management", True),
    ]
    clusters = ec.find_duplicate_candidates(items)
    by = {frozenset(it["id"] for it in c) for c in clusters}
    assert frozenset({1, 2, 3}) in by  # SBI variants merge
    assert frozenset({6, 7}) in by  # ARK variants merge
    # KB / LR are unrelated → must NOT be glued to SBI or each other
    clustered_ids = {it["id"] for c in clusters for it in c}
    assert 4 not in clustered_ids and 5 not in clustered_ids


def test_find_dupes_empty_when_all_distinct():
    items = [
        _it(1, "Tesla", "tesla", True),
        _it(2, "Foxconn", "foxconn", True),
        _it(3, "Elon Musk", "elon musk", False),
    ]
    assert ec.find_duplicate_candidates(items) == []


def test_load_staging_items_projection(session):
    from app.models.entity_catalog import EntityCatalogStaging

    session.add(
        EntityCatalogStaging(
            name="Tesla", name_normalised="tesla", is_org=True,
            description="EV maker", mentions_count=1,
        )
    )
    session.flush()
    items = ec.load_staging_items(session)
    assert len(items) == 1
    assert items[0]["name"] == "Tesla" and items[0]["is_org"] is True
    assert set(items[0]) == {"id", "name", "name_normalised", "is_org"}


def test_ingest_dry_run_makes_no_calls_and_no_writes(session, tmp_path, monkeypatch):
    from app.models.entity_catalog import EntityCatalogIngest, EntityCatalogStaging

    rows = [f"Org{i}\tx\tdesc {i}" for i in range(10)]
    path = _write(tmp_path, rows)

    def boom(*a, **k):
        raise AssertionError("dry-run must not call the LLM")

    monkeypatch.setattr(ec, "extract_entities_from_chunk", boom)
    stats = ec.ingest_source(
        session, path=path, llm_backend=None, model="m",
        max_chars=50, dry_run=True,
    )
    assert stats["chunks_processed"] > 0
    assert session.query(EntityCatalogStaging).count() == 0
    assert session.query(EntityCatalogIngest).count() == 0
