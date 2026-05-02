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
    """FR-CR-05-124 / FR-CR-05-128 — name normalisation must
    collapse common variants: case, whitespace, accents, legal
    forms, parenthetical notes, AND Cyrillic↔Latin variants of
    the same name (so the dedupe-by-name_normalised pull catches
    «Tencent» / «Тенсент» on one hub instead of two)."""
    assert normalise_name("ADNOC") == "adnoc"
    assert normalise_name("  ADNOC  ") == "adnoc"
    assert normalise_name("Goldman Sachs Inc.") == "goldman sachs"
    assert normalise_name("Goldman Sachs, LLC") == "goldman sachs"
    # FR-CR-05-128 — Cyrillic → Latin so cross-script duplicates
    # collapse. «Сбербанк ПАО» → «sberbank pao» → strip «pao» →
    # «sberbank».
    assert normalise_name("Сбербанк ПАО") == "sberbank"
    assert normalise_name("Газпром АО") == "gazprom"
    # Cyrillic-only entries land on Latin keys.
    assert normalise_name("Тенсент") == "tensent"  # phonetic key
    # Accents stripped.
    assert normalise_name("Société Générale") == "societe generale"
    # FR-CR-05-128 — parenthetical notes stripped (operator uses
    # parens for contact / source notes that vary across the
    # same canonical entity).
    assert normalise_name("Sequoia Capital (Лучиана)") == "sequoia capital"
    assert normalise_name("MGX Fund (via Guy Hamelin)") == "mgx fund"
    assert (
        normalise_name("BBB (British Business Bank) (via Brent)")
        == "bbb"
    )
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
    """FR-CR-05-124 — same canonical name twice on the same tab
    (operator typo / legal-form variants) collapses to one hub
    with one satellite (UNIQUE(counterparty_id, source) takes
    care of the second satellite)."""
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


def test_pull_dedupes_same_name_across_different_types(session, monkeypatch):
    """FR-CR-05-126 follow-up — operator regression: «Balderton»
    appears in both «Outreach» and «Rejections» tabs; «Tencent»
    in «Outreach» and «Strategic». Pre-fix the hub had two rows
    with same name but different `type`. Now one hub per
    canonical name, satellites per source so per-tab metadata
    still survives."""
    from app.sync import counterparties as _cp_mod

    full_data = {
        ("outreach-sheet", "Outreach"): [
            ["name"], ["Balderton"], ["Tencent"],
        ],
        ("outreach-sheet", "Rejections"): [
            ["name"], ["Balderton"],
        ],
        ("status-sheet", "Status outreach"): [
            ["type", "name"],
            ["Strategic", "Tencent"],
        ],
    }
    monkeypatch.setattr(
        _cp_mod, "build", lambda *a, **kw: _StubSheetsService(full_data)
    )
    sync = CounterpartiesSheetSync(
        credentials=object(),
        status_spreadsheet_id="status-sheet",
        status_tab_name="Status outreach",
        name_first_tabs=[
            ("outreach-sheet", "Outreach"),
            ("outreach-sheet", "Rejections"),
        ],
    )
    hubs, attrs = sync.pull(session)
    # 2 hubs (Balderton + Tencent), each with 2 satellites
    # (the two sources where they appeared).
    assert hubs == 2
    assert attrs == 4

    cp_rows = (
        session.query(Counterparty).order_by(Counterparty.name).all()
    )
    names = sorted(cp.name for cp in cp_rows)
    assert names == ["Balderton", "Tencent"]
    # Each canonical hub has multiple satellites pointing to it.
    balderton = next(c for c in cp_rows if c.name == "Balderton")
    sources = sorted(
        a.source for a in session.query(CounterpartyAttribute)
        .filter(CounterpartyAttribute.counterparty_id == balderton.id)
        .all()
    )
    assert sources == ["Outreach", "Rejections"]


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


def test_shortlist_translits_cyrillic_transcript_to_latin_directory(session):
    """FR-CR-05-126 follow-up — operator regression on a real
    Russian-language Zoom meeting: transcript said «шафлера»
    (Whisper for «Schaeffler») and «инвидио» (Whisper for
    «Nvidia»), but the directory has «Schaeffler» and «Nvidia»
    in Latin. SequenceMatcher between Cyrillic and Latin tokens
    rated those pairs at ~0.3 because the alphabets differ.
    Fix: transliterate Cyrillic → Latin before fuzzy match so
    «шафлера» → «shaflera» which scores ≥ 0.65 vs «schaeffler»."""
    from app.services.counterparty_match import (
        _shortlist_directory_for_transcript,
    )

    directory = [
        Counterparty(
            id=1, name="Schaeffler", type="Status outreach",
            name_normalised="schaeffler",
        ),
        Counterparty(
            id=2, name="Nvidia", type="Status outreach",
            name_normalised="nvidia",
        ),
        Counterparty(
            id=3, name="ADIA", type="Status outreach",
            name_normalised="adia",
        ),
        Counterparty(
            id=4, name="Random Distractor LLC", type="Outreach",
            name_normalised="random distractor",
        ),
    ]
    out = _shortlist_directory_for_transcript(
        directory,
        "обсудили шафлера, потом инвидио прислал ответ. эдия тоже на связи.",
    )
    out_ids = {cp.id for cp in out}
    assert 1 in out_ids, "Schaeffler not surfaced for «шафлера»"
    assert 2 in out_ids, "Nvidia not surfaced for «инвидио»"
    assert 3 in out_ids, "ADIA not surfaced for «эдия»"


def test_shortlist_catches_whisper_misheard_tokens(session):
    """FR-CR-05-126 — operator regression: «teaser» in the
    transcript is Whisper's misheard form of «Tether»; the
    fuzzy prefilter must surface Tether as a candidate so the
    LLM sees it. Plus a few other phonetic / Cyrillic cases."""
    from app.services.counterparty_match import (
        _shortlist_directory_for_transcript,
    )

    directory = [
        Counterparty(
            id=1, name="Tether", type="Status outreach",
            name_normalised="tether",
        ),
        Counterparty(
            id=2, name="ADNOC", type="Status outreach",
            name_normalised="adnoc",
        ),
        Counterparty(
            id=3, name="Goldman Sachs", type="Outreach",
            name_normalised="goldman sachs",
        ),
        Counterparty(
            id=4, name="Random Distractor LLC", type="Outreach",
            name_normalised="random distractor",
        ),
    ]
    # «teaser» (1-char swap from «tether») surfaces Tether.
    out = _shortlist_directory_for_transcript(
        directory,
        "обсудили teaser в раунде, надо выйти на инвестора",
    )
    out_ids = {cp.id for cp in out}
    assert 1 in out_ids, "Tether not surfaced for «teaser» misheard token"

    # Cyrillic «АДНОК» surfaces ADNOC via direct Latin token
    # «adnoc» being substring-equal after fold.
    out2 = _shortlist_directory_for_transcript(
        directory, "Adnoc делает pilot в нефтегазе.",
    )
    out2_ids = {cp.id for cp in out2}
    assert 2 in out2_ids

    # Direct mention of «Goldman».
    out3 = _shortlist_directory_for_transcript(
        directory, "Goldman прислали ответ по dataroom.",
    )
    out3_ids = {cp.id for cp in out3}
    assert 3 in out3_ids


def test_shortlist_falls_back_to_full_directory_when_empty(session):
    """FR-CR-05-126 — if the prefilter returns nothing, send the
    full directory. Better one extra LLM context than missing a
    match the model would otherwise catch."""
    from app.services.counterparty_match import (
        _shortlist_directory_for_transcript,
    )

    directory = [
        Counterparty(
            id=1, name="ADNOC", type="x", name_normalised="adnoc",
        ),
        Counterparty(
            id=2, name="Bosch", type="x", name_normalised="bosch",
        ),
    ]
    out = _shortlist_directory_for_transcript(
        directory, "Совершенно несвязанный текст без компаний.",
    )
    # Fallback: full directory returned (may be capped, but
    # nothing dropped).
    assert {cp.id for cp in out} == {1, 2}


def test_shortlist_keeps_phonetic_match_under_substring_noise(session):
    """FR-CR-05-128 — operator regression: 593-row directory +
    Russian transcript saying «Тезер» (phonetic for «Tether»)
    failed to surface Tether in the shortlist because hundreds
    of incidental substring boosts (any «X Capital» row gets
    0.95 from «капитал»→«kapital»→«apital» substring) pushed
    Tether's borderline 0.73 phonetic ratio past the 300-row
    cap. Cap raised to 500. Pin the inclusion so a future
    cap-tweak doesn't regress."""
    from app.models import Counterparty
    from app.services.counterparty_match import (
        _shortlist_directory_for_transcript,
    )

    # 400 «X Capital» distractors that all match «капитал» via
    # «kapital» → substring «apital» → 0.95 boost.
    distractors = [
        Counterparty(
            id=1000 + i, name=f"{name} Capital",
            type="Financial/VC", name_normalised=f"{name.lower()} capital",
        )
        for i, name in enumerate([f"Fund{i:03d}" for i in range(400)])
    ]
    tether = Counterparty(
        id=51396, name="Tether",
        type="Financial/VC", name_normalised="tether",
    )
    directory = distractors + [tether]
    transcript = (
        "Артем сказал что капитал у нас есть, надо сегментировать "
        "инвесторов и обязательно отправить апдейт Тезер по новым "
        "контрактам. Также обсудить капитал Bosch."
    )
    shortlist = _shortlist_directory_for_transcript(directory, transcript)
    in_short = [cp for cp in shortlist if cp.name == "Tether"]
    assert in_short, (
        f"Tether dropped from shortlist (size={len(shortlist)}); "
        f"top 5 names={[cp.name for cp in shortlist[:5]]}"
    )


def test_counterparty_match_prompt_pins_phonetic_and_cyrillic_examples():
    """FR-CR-05-126 — prompt includes the operator-regression
    worked examples («teaser/Tether», «АДНОК/ADNOC», «Голдман
    Сакс/Goldman Sachs») so a future prompt rewrite can't
    accidentally drop them."""
    from app.services.counterparty_match import (
        COUNTERPARTY_MATCH_SYSTEM,
    )

    blob = COUNTERPARTY_MATCH_SYSTEM
    assert "WHISPER MISHEARS" in blob or "Whisper" in blob
    assert "teaser" in blob and "Tether" in blob
    assert "АДНОК" in blob or "Адног" in blob
    assert "ADNOC" in blob
    assert "Cyrillic" in blob or "транслит" in blob.lower()


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
    # FR-CR-05-126 — the prompt now carries the FUZZY-PREFILTERED
    # shortlist, so transcript-mentioned names («ADNOC», «Bosch»)
    # appear; unrelated «Goldman Sachs» is correctly filtered out.
    assert "ADNOC" in backend.captured["user_prompt"]
    assert "Bosch" in backend.captured["user_prompt"]
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


def test_canonicalize_text_no_double_substring_cascade():
    """FR-CR-05-129 follow-up — operator regression: rewriting
    «Insight Partners ...» with mapping «Insight → Insight
    Partners» produced «Insight Partners Partners» (cascade
    substring). Fix: regex with negative lookahead against the
    canonical's tail when the mention is a prefix of canonical.

    Other replaces (mention ≠ prefix of canonical) keep their
    word-boundary behaviour."""
    from app.services.counterparty_match import canonicalize_text

    mapping = {
        "Insight": "Insight Partners",
        "Тезер": "Tether",
        "Bauer/Dart": "Bauerdart",
    }
    # «Insight Partners» already canonical → no double.
    assert canonicalize_text(
        "подготовить follow-up Insight Partners по отказу", mapping
    ) == "подготовить follow-up Insight Partners по отказу"
    # «Insight» alone → expanded.
    assert canonicalize_text(
        "Insight отказали", mapping
    ) == "Insight Partners отказали"
    # Cyrillic mention → Latin canonical.
    assert canonicalize_text(
        "отправить апдейт Тезер", mapping
    ) == "отправить апдейт Tether"
    # «Bauer/Dart» (slash-rendered) → canonical without slash.
    assert canonicalize_text(
        "Bauer/Dart - пригласить в офис", mapping
    ) == "Bauerdart - пригласить в офис"


def test_fuzzy_extend_handles_composite_slash_tokens():
    """FR-CR-05-129 follow-up — operator regression: extract
    LLM combined phonetic variants with `/` («Jamal/Jabal»,
    «Boutert/Bauerdart»). The composite-token regex captures
    the whole thing and ratio against single-name canonical
    («jabal» 5 chars vs «jamal/jabal» 11 chars) bombs by
    length-diff filter.

    Fix: split composite tokens on `/`, `-`; fuzzy-match each
    piece; pick best canonical; map the WHOLE composite to
    that canonical so canonicalize_text rewrites in one shot."""
    from app.models import Counterparty
    from app.services.counterparty_match import fuzzy_extend_canonical_map

    directory = [
        Counterparty(id=1, name="Jabal", type="VC", name_normalised="jabal"),
        Counterparty(id=2, name="Bauerdart", type="VC", name_normalised="bauerdart"),
    ]
    text = (
        "Уточнить график demo с Jamal/Jabal в Лондоне. "
        "Пригласить Boutert/Bauerdart на demo."
    )
    extended = fuzzy_extend_canonical_map(text, directory, {})
    assert "Jamal/Jabal" in extended
    assert extended["Jamal/Jabal"] == "Jabal"
    assert "Boutert/Bauerdart" in extended
    assert extended["Boutert/Bauerdart"] == "Bauerdart"


def test_fuzzy_extend_canonical_map_catches_jamal_jabal_class():
    """FR-CR-05-129 follow-up — operator regression: the
    transcript-side LLM Pass 1 caught «Jabal» but the separate
    task-extract LLM later wrote «Jamal» in a task description.
    Pass-2 mapping had `{Jabal: Jabal}` only — canonicalize
    didn't rewrite «Jamal». Python fuzzy fallback runs over
    task content tokens and adds entries for any directory
    name within ratio ≥ 0.8 (post-translit)."""
    from app.models import Counterparty
    from app.services.counterparty_match import fuzzy_extend_canonical_map

    directory = [
        Counterparty(id=1, name="Jabal", type="VC", name_normalised="jabal"),
        Counterparty(id=2, name="Tether", type="VC", name_normalised="tether"),
        Counterparty(id=3, name="Bauerdart", type="VC", name_normalised="bauerdart"),
        Counterparty(id=4, name="Schaeffler", type="VC", name_normalised="schaeffler"),
    ]
    text = (
        "Уточнить график демо с Jamal в Лондоне. "
        "Пригласить Бауэрдарта на демо."
    )
    extended = fuzzy_extend_canonical_map(text, directory, {})
    # Jamal → Jabal (ratio 0.8, the operator regression).
    assert "Jamal" in extended
    assert extended["Jamal"] == "Jabal"
    # Бауэрдарта (declined Cyrillic) → Bauerdart (ratio ~0.84).
    assert any(v == "Bauerdart" for v in extended.values())
    # Generic verbs / nouns (capitalized at sentence start) NOT
    # added to mapping — fuzzy ratio against company names is
    # too low. «Уточнить», «Пригласить» etc. don't match.
    assert "Уточнить" not in extended
    assert "Пригласить" not in extended


def test_fuzzy_extend_preserves_existing_map():
    """Existing entries (from Pass 2 LLM) must not be
    overwritten or removed by the fuzzy pass."""
    from app.models import Counterparty
    from app.services.counterparty_match import fuzzy_extend_canonical_map

    directory = [
        Counterparty(id=1, name="Tether", type="VC", name_normalised="tether"),
    ]
    existing = {"Тезер": "Tether"}
    extended = fuzzy_extend_canonical_map("blah blah", directory, existing)
    assert extended == existing
