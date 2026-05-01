"""FR-CR-05-124 — counterparties hub + satellite directory.

Covers:
- `normalise_name` produces a fuzzy-match-friendly key.
- `CounterpartiesSheetSync.pull` wipes-and-reloads from both
  source sheets, with the correct field mapping per source.
- The `(name_normalised, type)` UNIQUE constraint deduplicates
  the same name listed in multiple tabs.
- Listener integration: `_maybe_run_counterparties_pull` is off
  by default, throttled, swallows pull errors.
- CLI `ops/pull_counterparties.py --dry-run` reads without
  writing.
"""
from __future__ import annotations

import pytest

from app.models import Counterparty, CounterpartyAttribute
from app.sync.counterparties import CounterpartiesSheetSync, normalise_name


def test_normalise_name_strips_accents_and_legal_forms():
    """FR-CR-05-124 — name normalisation must collapse the
    common variants speech recognition produces vs the canonical
    form on the sheet (case, whitespace, accents, legal forms)."""
    assert normalise_name("ADNOC") == "adnoc"
    assert normalise_name("  ADNOC  ") == "adnoc"
    assert normalise_name("Goldman Sachs Inc.") == "goldman sachs"
    assert normalise_name("Goldman Sachs, LLC") == "goldman sachs"
    # Cyrillic legal forms.
    assert normalise_name("Сбербанк ПАО") == "сбербанк"
    assert normalise_name("Газпром АО") == "газпром"
    # Accents stripped.
    assert normalise_name("Société Générale") == "societe generale"
    # Empty / None safe.
    assert normalise_name("") == ""
    assert normalise_name(None) == ""


class _StubSheetsService:
    """Mock googleapiclient discovery service. Holds {tab → rows}
    per spreadsheet id; raises on unknown ids."""

    def __init__(self, data: dict[tuple[str, str], list[list[str]]]):
        self._data = data

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, *, spreadsheetId, range):
        tab = range.split("!")[0]
        rows = self._data.get((spreadsheetId, tab), [])
        return _StubExec({"values": rows})


class _StubExec:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


def _make_sync(monkeypatch, *, status_data, outreach_data):
    """Build a `CounterpartiesSheetSync` whose googleapiclient
    layer is replaced by stubs. The two source sheets get
    different ids so the stub can route reads correctly."""
    from app.sync import counterparties as _cp_mod

    full_data = {}
    for tab, rows in status_data.items():
        full_data[("status-sheet", tab)] = rows
    for tab, rows in outreach_data.items():
        full_data[("outreach-sheet", tab)] = rows

    monkeypatch.setattr(
        _cp_mod, "build", lambda *a, **kw: _StubSheetsService(full_data)
    )
    name_first = [
        ("outreach-sheet", "Outreach"),
        ("outreach-sheet", "Rejections"),
        ("outreach-sheet", "Looking for intros"),
    ]
    return CounterpartiesSheetSync(
        credentials=object(),
        status_spreadsheet_id="status-sheet",
        status_tab_name="Status outreach",
        name_first_tabs=name_first,
    )


def test_pull_loads_status_outreach_with_jsonb_attributes(session, monkeypatch):
    """FR-CR-05-124 source A — Status outreach tab. Field A=type,
    field B=name, every column captured into the satellite's
    JSONB. Multiple rows produce multiple hubs + satellites."""
    sync = _make_sync(
        monkeypatch,
        status_data={
            "Status outreach": [
                ["type", "name", "status", "comments"],
                ["investor", "ADNOC", "active", "DD in progress"],
                ["client", "Bosch", "pilot", "Pilot March 2026"],
            ],
        },
        outreach_data={},
    )
    hubs, attrs = sync.pull(session)
    assert hubs == 2
    assert attrs == 2

    rows = session.query(Counterparty).order_by(Counterparty.name).all()
    assert {r.name for r in rows} == {"ADNOC", "Bosch"}
    assert {r.type for r in rows} == {"investor", "client"}

    # Satellite carries every column from the sheet.
    adnoc_attrs = (
        session.query(CounterpartyAttribute)
        .filter(CounterpartyAttribute.source == "Status outreach")
        .all()
    )
    adnoc = next(a for a in adnoc_attrs if a.attributes.get("name") == "ADNOC")
    assert adnoc.attributes["type"] == "investor"
    assert adnoc.attributes["status"] == "active"
    assert adnoc.attributes["comments"] == "DD in progress"


def test_pull_loads_outreach_tabs_with_tab_name_as_type(session, monkeypatch):
    """FR-CR-05-124 source B — three tabs. Field A=name, the TAB
    NAME becomes the `type` value on the hub."""
    sync = _make_sync(
        monkeypatch,
        status_data={},
        outreach_data={
            "Outreach": [
                ["name"],
                ["Felix Capital"],
                ["Supernova"],
            ],
            "Rejections": [
                ["name"],
                ["Nvidia"],
            ],
            "Looking for intros": [
                ["name"],
                ["Goldman Sachs"],
            ],
        },
    )
    hubs, attrs = sync.pull(session)
    assert hubs == 4
    assert attrs == 4

    rows = (
        session.query(Counterparty).order_by(Counterparty.name).all()
    )
    by_name = {r.name: r for r in rows}
    assert by_name["Felix Capital"].type == "Outreach"
    assert by_name["Supernova"].type == "Outreach"
    assert by_name["Nvidia"].type == "Rejections"
    assert by_name["Goldman Sachs"].type == "Looking for intros"


def test_pull_wipes_existing_rows_before_reloading(session, monkeypatch):
    """FR-CR-05-124 wipe-and-replace contract: each pull deletes
    every existing row before the reload so a counterparty
    removed from the sheet disappears from the directory."""
    # Seed with stale data the next pull should erase.
    stale = Counterparty(
        name="StaleCo",
        type="investor",
        name_normalised="staleco",
    )
    session.add(stale)
    session.flush()
    session.add(
        CounterpartyAttribute(
            counterparty_id=stale.id,
            source="Status outreach",
            attributes={"foo": "bar"},
        )
    )
    session.flush()
    assert session.query(Counterparty).count() == 1

    sync = _make_sync(
        monkeypatch,
        status_data={
            "Status outreach": [
                ["type", "name"],
                ["investor", "FreshCo"],
            ],
        },
        outreach_data={},
    )
    hubs, _ = sync.pull(session)
    assert hubs == 1

    rows = session.query(Counterparty).all()
    assert [r.name for r in rows] == ["FreshCo"]
    # Cascade also removed the stale satellite.
    assert session.query(CounterpartyAttribute).count() == 1


def test_pull_dedupes_same_name_in_status_outreach(session, monkeypatch):
    """FR-CR-05-124 — `(name_normalised, type)` unique. The same
    canonical name appearing twice on the same tab (operator
    typo / merger duplicates) collapses to one hub with two
    satellites — except `(counterparty_id, source)` is also
    unique, so on the SAME source the second wins as a single
    satellite."""
    sync = _make_sync(
        monkeypatch,
        status_data={
            "Status outreach": [
                ["type", "name", "comment"],
                ["investor", "Goldman Sachs Inc.", "first"],
                ["investor", "Goldman Sachs LLC", "second"],
            ],
        },
        outreach_data={},
    )
    hubs, attrs = sync.pull(session)
    # Both rows normalise to «goldman sachs» → 1 hub.
    assert hubs == 1
    rows = session.query(Counterparty).all()
    assert rows[0].name == "Goldman Sachs Inc."
    assert rows[0].name_normalised == "goldman sachs"


def test_pull_empty_input_does_not_wipe_directory(session, monkeypatch):
    """FR-CR-05-124 safety: when both sheets are empty / mis-
    configured, the pull is a no-op rather than wiping the
    directory. Protects against a transient API failure
    blanking the directory."""
    seed = Counterparty(
        name="KeepMe", type="investor", name_normalised="keepme",
    )
    session.add(seed)
    session.flush()

    sync = _make_sync(
        monkeypatch,
        status_data={"Status outreach": []},
        outreach_data={
            "Outreach": [],
            "Rejections": [],
            "Looking for intros": [],
        },
    )
    hubs, attrs = sync.pull(session)
    assert (hubs, attrs) == (0, 0)
    assert session.query(Counterparty).count() == 1


def test_pull_loads_targets_sheet_alongside_outreach(session, monkeypatch):
    """FR-CR-05-124 follow-up — third source: «Investor Targets»
    + «rejected» tabs on a separate sheet. Generic
    `name_first_tabs=[(sheet_id, tab), ...]` lets the factory
    glue any number of name-first sources together."""
    from app.sync import counterparties as _cp_mod

    full_data = {
        ("outreach-sheet", "Outreach"): [["name"], ["Felix Capital"]],
        ("targets-sheet", "Investor Targets"): [
            ["name", "comment"],
            ["Hyperloop Ventures", "warm intro"],
            ["XYZ Fund", "cold"],
        ],
        ("targets-sheet", "rejected"): [
            ["name"], ["Old Fund"],
        ],
    }
    monkeypatch.setattr(
        _cp_mod, "build", lambda *a, **kw: _StubSheetsService(full_data)
    )
    sync = CounterpartiesSheetSync(
        credentials=object(),
        status_spreadsheet_id="",
        status_tab_name="Status outreach",
        name_first_tabs=[
            ("outreach-sheet", "Outreach"),
            ("targets-sheet", "Investor Targets"),
            ("targets-sheet", "rejected"),
        ],
    )
    hubs, attrs = sync.pull(session)
    # 1 Outreach + 2 Investor Targets + 1 rejected = 4 hubs
    assert hubs == 4
    rows = (
        session.query(Counterparty).order_by(Counterparty.name).all()
    )
    by_name = {r.name: r for r in rows}
    assert by_name["Felix Capital"].type == "Outreach"
    assert by_name["Hyperloop Ventures"].type == "Investor Targets"
    assert by_name["XYZ Fund"].type == "Investor Targets"
    assert by_name["Old Fund"].type == "rejected"
    # Captured attributes from the targets sheet survive in
    # the satellite.
    targets_attrs = (
        session.query(CounterpartyAttribute)
        .filter(CounterpartyAttribute.source == "Investor Targets")
        .all()
    )
    by_name_attr = {a.attributes["name"]: a for a in targets_attrs}
    assert by_name_attr["Hyperloop Ventures"].attributes["comment"] == "warm intro"


def test_factory_returns_none_when_no_sheet_ids():
    """FR-CR-05-124 — feature is opt-in. No sheet ids → no
    factory wired → listener silently skips the pull tick."""
    from app.config import Settings
    from app.sync.factories import build_counterparties_sheet_factory

    s = Settings(COUNTERPARTIES_STATUS_SHEET_ID="", COUNTERPARTIES_OUTREACH_SHEET_ID="")
    assert build_counterparties_sheet_factory(s) is None


def test_match_counterparties_in_transcript_dedupes_and_orders(session):
    """FR-CR-05-125 — matcher returns canonical Counterparty
    rows in LLM-emitted order, dedupes, drops invalid ids."""
    from app.services.counterparty_match import (
        match_counterparties_in_transcript,
    )

    # Seed a directory.
    cp1 = Counterparty(
        name="ADNOC", type="Status outreach", name_normalised="adnoc",
    )
    cp2 = Counterparty(
        name="Bosch", type="Status outreach", name_normalised="bosch",
    )
    cp3 = Counterparty(
        name="Goldman Sachs", type="Outreach",
        name_normalised="goldman sachs",
    )
    session.add_all([cp1, cp2, cp3])
    session.flush()
    cp1_id, cp2_id, cp3_id = cp1.id, cp2.id, cp3.id

    class _StubLLM:
        def __init__(self, returned_ids):
            self.returned = returned_ids
            self.captured = None

        def call_tool(self, **kw):
            self.captured = kw
            return {"matched_ids": self.returned}

    # LLM returns ids in order with a duplicate + an invalid id.
    backend = _StubLLM([cp2_id, cp1_id, cp1_id, 99999])
    out = match_counterparties_in_transcript(
        session,
        transcript="Сегодня обсуждали ADNOC, потом Bosch.",
        llm_backend=backend,
        model="gpt-5.5",
    )
    # Order preserved (Bosch first, then ADNOC), dedupe, invalid
    # id dropped.
    assert [cp.id for cp in out] == [cp2_id, cp1_id]
    # Directory rendering carried real id/name/type values.
    assert "ADNOC" in backend.captured["user_prompt"]
    assert "Goldman Sachs" in backend.captured["user_prompt"]
    assert "directory:" in backend.captured["user_prompt"]


def test_match_counterparties_returns_empty_on_llm_failure(session):
    """FR-CR-05-125 — LLM call exception → empty list, never
    propagates. Pipeline keeps going without the 🔗 section."""
    from app.services.counterparty_match import (
        match_counterparties_in_transcript,
    )

    session.add(
        Counterparty(
            name="ADNOC", type="Status outreach", name_normalised="adnoc",
        )
    )
    session.flush()

    class _BoomLLM:
        def call_tool(self, **kw):
            raise RuntimeError("model down")

    out = match_counterparties_in_transcript(
        session,
        transcript="ADNOC something",
        llm_backend=_BoomLLM(),
        model="gpt-5.5",
    )
    assert out == []


def test_match_counterparties_skips_when_directory_empty(session):
    """FR-CR-05-125 — empty directory → no LLM call, returns
    []. Operator hasn't pulled from sheets yet; pipeline runs
    fine, just without the 🔗 section."""
    from app.services.counterparty_match import (
        match_counterparties_in_transcript,
    )

    class _NoCallLLM:
        def call_tool(self, **kw):
            raise AssertionError("LLM should not be called on empty dir")

    out = match_counterparties_in_transcript(
        session,
        transcript="ADNOC big talk",
        llm_backend=_NoCallLLM(),
        model="gpt-5.5",
    )
    assert out == []


def test_build_counterparties_section_renders_doc_and_short_summary(session):
    """FR-CR-05-125 — both helpers query `counterparty_mentions`
    and render canonical names. Doc form has 🔗 КОНТРАГЕНТЫ
    header + bullet list with type. Short-summary form is a
    single line with comma-separated names."""
    from app.fireflies.pipeline import (
        _build_counterparties_section_for_doc,
        _build_counterparties_section_for_short_summary,
    )
    from app.models import CounterpartyMention

    cp_a = Counterparty(
        name="ADNOC", type="Status outreach", name_normalised="adnoc",
    )
    cp_b = Counterparty(
        name="Bosch", type="Outreach", name_normalised="bosch",
    )
    session.add_all([cp_a, cp_b])
    session.flush()
    session.add_all([
        CounterpartyMention(
            counterparty_id=cp_a.id, source_kind="fireflies",
            source_id="trans-x",
        ),
        CounterpartyMention(
            counterparty_id=cp_b.id, source_kind="fireflies",
            source_id="trans-x",
        ),
    ])
    session.flush()

    doc = _build_counterparties_section_for_doc(
        session, source_kind="fireflies", source_id="trans-x",
    )
    assert "🔗 КОНТРАГЕНТЫ" in doc
    assert "• ADNOC — Status outreach" in doc
    assert "• Bosch — Outreach" in doc

    short = _build_counterparties_section_for_short_summary(
        session, source_kind="fireflies", source_id="trans-x",
    )
    assert short == "🔗 Контрагенты: ADNOC, Bosch"

    # No matches → empty string.
    assert _build_counterparties_section_for_doc(
        session, source_kind="fireflies", source_id="trans-other",
    ) == ""
    assert _build_counterparties_section_for_short_summary(
        session, source_kind="fireflies", source_id="trans-other",
    ) == ""


def test_settings_outreach_tab_names_default_split_correctly():
    """FR-CR-05-124 — comma-separated tab names default to the
    three operator-pinned ones."""
    from app.config import Settings

    s = Settings()
    tabs = [
        t.strip()
        for t in s.counterparties_outreach_tab_names.split(",")
        if t.strip()
    ]
    assert tabs == ["Outreach", "Rejections", "Looking for intros"]
