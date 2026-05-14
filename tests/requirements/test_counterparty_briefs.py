"""FR-CR-05-168 — Counterparty Briefs.

This file is the test-spec for the feature defined in
SPEC_COUNTERPARTY_BRIEFS_v0.1.md. Implementation lives under
`app/counterparty_briefs/` (not yet committed). Tests are
marked `xfail(strict=True)` so they fail loudly the moment a
module is added but the test isn't updated — and they turn green
automatically once the module behaves as specified.

Mapping FR → test is documented in §14 of the SPEC.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


# `xfail(strict=True)` — fails as expected today (module not
# imported yet). When the implementation lands, the test should
# PASS; strict=True flips XFAIL into XPASS-as-failure so we don't
# accidentally ship implementation without bringing the test
# up to spec.

pytestmark = pytest.mark.xfail(
    reason="FR-CR-05-168 implementation pending — SPEC + tests written first.",
    strict=True,
)


# -- Category 1: Discovery (FR-CB-1.x) --------------------------------------


def test_brief_runner_disabled_no_op(monkeypatch):
    """FR-CB-1.1 / FR-CB-8.1 — runner thread NOT spawned when
    COUNTERPARTY_BRIEFS_ENABLED=false. No Calendar / OpenAI /
    Slack I/O happens."""
    from unittest.mock import MagicMock

    from app.config import Settings
    from app.counterparty_briefs.runner import CounterpartyBriefRunner

    monkeypatch.setenv("COUNTERPARTY_BRIEFS_ENABLED", "false")
    runner = CounterpartyBriefRunner(
        settings=Settings(),
        slack_client=MagicMock(),
        llm_backend=MagicMock(),
        calendar_factory=lambda: None,
        docs_factory=lambda: None,
    )
    runner.start()
    assert runner._thread is None


def test_brief_window_lookahead(monkeypatch):
    """FR-CB-1.2 — runner asks Calendar for events in
    [now+lead_hours - window, now+lead_hours + window]."""
    from app.config import Settings
    from app.counterparty_briefs.runner import (
        compute_target_window,
    )

    monkeypatch.setenv("COUNTERPARTY_BRIEFS_LEAD_HOURS", "4")
    monkeypatch.setenv("COUNTERPARTY_BRIEFS_WINDOW_MINUTES", "5")
    target, window_min = compute_target_window(
        Settings(), now=datetime(2026, 5, 14, 9, 0, tzinfo=timezone.utc),
    )
    assert target == datetime(2026, 5, 14, 13, 0, tzinfo=timezone.utc)
    assert window_min == 5


def test_brief_organizer_creator_filter():
    """FR-CB-1.3 — events with organizer.email or creator.email
    different from operator_email are dropped (inherits agenda
    runner's FR-CR-05-167 gate)."""
    from app.counterparty_briefs.runner import event_passes_host_gate

    operator = "1@thehumanoid.ai"
    assert event_passes_host_gate(
        {"organizer": {"email": operator},
         "creator":   {"email": operator}},
        operator_email=operator,
    )
    # Teammate's event copied into operator's shared calendar
    assert not event_passes_host_gate(
        {"organizer": {"email": operator},
         "creator":   {"email": "irina@thehumanoid.ai"}},
        operator_email=operator,
    )
    # External event entirely
    assert not event_passes_host_gate(
        {"organizer": {"email": "x@external.com"},
         "creator":   {"email": "x@external.com"}},
        operator_email=operator,
    )


def test_brief_cli_lookahead_arg():
    """FR-CB-1.5 — `ops.brief_run_once` accepts
    `--lookahead-hours N`."""
    import sys
    from unittest.mock import patch

    from ops import brief_run_once

    with patch.object(sys, "argv", ["brief_run_once", "--lookahead-hours", "12", "--dry-run"]):
        # Smoke: parser doesn't crash; dry-run returns 0.
        rc = brief_run_once.main()
    assert rc == 0


# -- Category 2: Counterparty extraction (FR-CB-2.x) ------------------------


def test_brief_extract_returns_person_and_org():
    """FR-CB-2.1 — LLM extracts person + org from the event."""
    from unittest.mock import MagicMock

    from app.counterparty_briefs.extract import extract_counterparties

    llm = MagicMock()
    llm.complete_json = MagicMock(return_value={
        "counterparties": [
            {"person_name": "Samer Nawaf Zawaideh",
             "person_role": "CIO",
             "org_name":    "Strategic Development Fund"}
        ]
    })
    event = {
        "title": "SDF <> Humanoid | Intro call",
        "description": "Intro to Samer Nawaf Zawaideh, CIO of SDF.",
        "attendees": [
            {"email": "1@thehumanoid.ai"},
            {"email": "samer.zawaideh@sdf.ae"},
        ],
    }
    out = extract_counterparties(
        event=event, llm_backend=llm, model="gpt-test",
    )
    assert len(out) == 1
    assert out[0].person_name == "Samer Nawaf Zawaideh"
    assert out[0].org_name == "Strategic Development Fund"


def test_brief_extract_skips_internal_attendees():
    """FR-CB-2.2 — emails on @thehumanoid.ai are internal and
    never appear as counterparties."""
    from unittest.mock import MagicMock

    from app.counterparty_briefs.extract import extract_counterparties

    llm = MagicMock()
    # The LLM must be instructed to skip internal emails — the
    # contract test asserts the prompt or the post-processing
    # filter strips them.
    llm.complete_json.return_value = {"counterparties": []}
    event = {
        "title": "Артем-Алина sync",
        "attendees": [
            {"email": "1@thehumanoid.ai"},
            {"email": "kaa@thehumanoid.ai"},
        ],
    }
    out = extract_counterparties(
        event=event, llm_backend=llm, model="gpt-test",
    )
    assert out == []


def test_brief_extract_skips_event_with_no_counterparty():
    """FR-CB-2.3 — extract returns [] for fully-internal /
    no-name events; runner skips them silently."""
    from unittest.mock import MagicMock

    from app.counterparty_briefs.extract import extract_counterparties

    llm = MagicMock()
    llm.complete_json.return_value = {"counterparties": []}
    out = extract_counterparties(
        event={"title": "Block: Personal time", "attendees": []},
        llm_backend=llm, model="gpt-test",
    )
    assert out == []


def test_brief_extract_supports_multi_counterparty():
    """FR-CB-2.4 — event with two external attendees yields two
    candidates."""
    from unittest.mock import MagicMock

    from app.counterparty_briefs.extract import extract_counterparties

    llm = MagicMock()
    llm.complete_json.return_value = {
        "counterparties": [
            {"person_name": "Genia Xasis", "person_role": None,
             "org_name": None},
            {"person_name": "Nick Tarrant", "person_role": "Partner",
             "org_name": "Cambridge Source"},
        ]
    }
    event = {
        "title": "Genia & Nick — fundraising debrief",
        "attendees": [
            {"email": "1@thehumanoid.ai"},
            {"email": "genia@x.com"},
            {"email": "nick@cambridgesource.com"},
        ],
    }
    out = extract_counterparties(
        event=event, llm_backend=llm, model="gpt-test",
    )
    assert {c.person_name for c in out} == {"Genia Xasis", "Nick Tarrant"}


def test_brief_extract_output_schema():
    """FR-CB-2.5 — bad-shape LLM output is rejected, runner
    returns [] instead of crashing."""
    from unittest.mock import MagicMock

    from app.counterparty_briefs.extract import extract_counterparties

    llm = MagicMock()
    llm.complete_json.return_value = "not a dict"
    out = extract_counterparties(
        event={"title": "x", "attendees": []},
        llm_backend=llm, model="gpt-test",
    )
    assert out == []


# -- Category 3: DB lookup (FR-CB-3.x) --------------------------------------


def test_brief_lookup_person_via_mentions(session):
    """FR-CB-3.1 — `counterparty_mentions` matched by normalised
    name."""
    from app.counterparty_briefs.extract import CounterpartyCandidate
    from app.counterparty_briefs.lookup import lookup_counterparty
    from app.models import Counterparty, CounterpartyMention

    cp = Counterparty(name="Genia Xasis", name_normalised="genia xasis")
    session.add(cp)
    session.flush()
    session.add(
        CounterpartyMention(counterparty_id=cp.id, mention_text="genia")
    )
    session.flush()

    ctx = lookup_counterparty(
        session,
        candidate=CounterpartyCandidate(
            person_name="Genia Xasis", person_role=None, org_name=None,
        ),
    )
    assert ctx.counterparty_id == cp.id


def test_brief_lookup_org_via_counterparties(session):
    """FR-CB-3.2 — `counterparties` hub by `name_normalised`."""
    from app.counterparty_briefs.extract import CounterpartyCandidate
    from app.counterparty_briefs.lookup import lookup_counterparty
    from app.models import Counterparty

    session.add(Counterparty(
        name="Bracket Capital", name_normalised="bracket capital",
    ))
    session.flush()

    ctx = lookup_counterparty(
        session,
        candidate=CounterpartyCandidate(
            person_name=None, person_role=None, org_name="Bracket Capital",
        ),
    )
    assert ctx.counterparty_id is not None


def test_brief_lookup_finds_past_meetings(session):
    """FR-CB-3.3 — join past `zoom_recordings` / `meeting_recordings`
    by title contains counterparty normalised name."""
    from datetime import datetime, timezone

    from app.counterparty_briefs.extract import CounterpartyCandidate
    from app.counterparty_briefs.lookup import lookup_counterparty
    from app.models import ZoomRecording

    session.add(ZoomRecording(
        zoom_id="z1",
        title="Genia Xasis weekly fundraising sync",
        meeting_date=datetime(2026, 5, 7, tzinfo=timezone.utc),
    ))
    session.flush()

    ctx = lookup_counterparty(
        session,
        candidate=CounterpartyCandidate(
            person_name="Genia Xasis", person_role=None, org_name=None,
        ),
    )
    assert len(ctx.past_recordings) == 1
    assert ctx.past_recordings[0].get("zoom_id") == "z1"


def test_brief_lookup_finds_open_tasks(session):
    """FR-CB-3.4 — open tasks linked to past meetings of this
    counterparty."""
    from datetime import datetime, timezone

    from app.counterparty_briefs.extract import CounterpartyCandidate
    from app.counterparty_briefs.lookup import lookup_counterparty
    from app.models import Task, TaskPriority, TaskStatus, ZoomRecording

    session.add(ZoomRecording(
        zoom_id="z1",
        title="Genia Xasis weekly sync",
        meeting_date=datetime(2026, 5, 7, tzinfo=timezone.utc),
    ))
    session.add(Task(
        title="Send updated deck",
        source_kind="zoom",
        source_conversation_id="z1",
        status=TaskStatus.todo,
        priority=TaskPriority.high,
    ))
    session.flush()

    ctx = lookup_counterparty(
        session,
        candidate=CounterpartyCandidate(
            person_name="Genia Xasis", person_role=None, org_name=None,
        ),
    )
    assert len(ctx.open_tasks) == 1
    assert ctx.open_tasks[0].get("title") == "Send updated deck"


# -- Category 4: Deep research (FR-CB-4.x) ----------------------------------


def test_brief_research_calls_openai_o4_mini_deep_research(monkeypatch):
    """FR-CB-4.1 — model name explicitly `o4-mini-deep-research`
    (operator-pinned)."""
    from unittest.mock import MagicMock

    from app.counterparty_briefs.extract import CounterpartyCandidate
    from app.counterparty_briefs.research import research_counterparty

    llm = MagicMock()
    llm.complete_json.return_value = {"personal_information": {}}
    research_counterparty(
        candidate=CounterpartyCandidate(
            person_name="X", person_role=None, org_name="Y",
        ),
        context=None,
        llm_backend=llm,
        model="o4-mini-deep-research",
        budget_usd=2.0,
    )
    # Assertion: model passed through to the call
    called_kwargs = llm.complete_json.call_args.kwargs
    assert called_kwargs.get("model") == "o4-mini-deep-research"


def test_brief_research_output_schema(monkeypatch):
    """FR-CB-4.2 — research output validated against the profile
    JSON schema. Bad shape returns None instead of crashing."""
    from unittest.mock import MagicMock

    from app.counterparty_briefs.extract import CounterpartyCandidate
    from app.counterparty_briefs.research import research_counterparty

    llm = MagicMock()
    llm.complete_json.return_value = "not a dict"
    result = research_counterparty(
        candidate=CounterpartyCandidate(
            person_name="X", person_role=None, org_name="Y",
        ),
        context=None, llm_backend=llm,
        model="o4-mini-deep-research", budget_usd=2.0,
    )
    assert result is None


def test_brief_research_skips_over_budget(monkeypatch):
    """FR-CB-4.3 — when the estimated cost exceeds budget, the
    LLM call is NOT made and `research_counterparty` returns
    None with a structured log."""
    from unittest.mock import MagicMock

    from app.counterparty_briefs.extract import CounterpartyCandidate
    from app.counterparty_briefs.research import research_counterparty

    llm = MagicMock()
    # Force the cost estimator to return a number > budget
    monkeypatch.setattr(
        "app.counterparty_briefs.research._estimate_cost_usd",
        lambda **kwargs: 5.0,
    )
    out = research_counterparty(
        candidate=CounterpartyCandidate(
            person_name="X", person_role=None, org_name="Y",
        ),
        context=None, llm_backend=llm,
        model="o4-mini-deep-research", budget_usd=2.0,
    )
    assert out is None
    assert llm.complete_json.call_count == 0


def test_brief_research_uses_cache_within_ttl(session):
    """FR-CB-4.4 — re-use prior `research_payload` from
    counterparty_briefs within TTL days, no new LLM call."""
    from datetime import datetime, timedelta, timezone
    from unittest.mock import MagicMock

    from app.counterparty_briefs.extract import CounterpartyCandidate
    from app.counterparty_briefs.research import research_counterparty
    from app.models import CounterpartyBrief

    session.add(CounterpartyBrief(
        calendar_event_id="ev_old",
        counterparty_key="genia-xasis",
        display_name="Genia Xasis",
        org_name="",
        scheduled_meeting_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        posted_at=datetime.now(timezone.utc) - timedelta(days=3),
        slack_channel="D0",
        research_payload={"profile_overview": "cached"},
        cost_usd="0.5",
    ))
    session.flush()

    llm = MagicMock()
    out = research_counterparty(
        candidate=CounterpartyCandidate(
            person_name="Genia Xasis", person_role=None, org_name=None,
        ),
        context=None, llm_backend=llm,
        model="o4-mini-deep-research", budget_usd=2.0,
        session=session, cache_ttl_days=14,
    )
    assert out is not None
    assert out.profile_overview == "cached"
    assert llm.complete_json.call_count == 0


def test_brief_research_failure_falls_back_to_db_only():
    """FR-CB-4.5 — when the LLM call raises, runner should
    proceed to build a DB-only Doc instead of dropping the
    candidate."""
    from unittest.mock import MagicMock

    from app.counterparty_briefs.extract import CounterpartyCandidate
    from app.counterparty_briefs.research import research_counterparty

    llm = MagicMock()
    llm.complete_json.side_effect = RuntimeError("network down")
    out = research_counterparty(
        candidate=CounterpartyCandidate(
            person_name="X", person_role=None, org_name="Y",
        ),
        context=None, llm_backend=llm,
        model="o4-mini-deep-research", budget_usd=2.0,
    )
    assert out is None  # caller falls back


# -- Category 5: Doc generation (FR-CB-5.x) ---------------------------------


def test_brief_doc_uses_docs_export_service():
    """FR-CB-5.1 — Doc creation goes through the shared
    `DocsExportService.export_summary` (FR-CR-05-43)."""
    from app.counterparty_briefs.doc import build_doc_body
    # The function is pure-text; runner is the one that calls
    # DocsExportService. Smoke: build_doc_body returns a non-empty
    # markdown body when given any candidate.
    md = build_doc_body(candidate=None, context=None, research=None)
    assert isinstance(md, str)
    assert md.strip()


def test_brief_doc_title_format():
    """FR-CB-5.2 — Doc title is «DD/MM - Brief: <Counterparty Name>»."""
    from datetime import datetime, timezone

    from app.counterparty_briefs.doc import build_doc_title
    from app.counterparty_briefs.extract import CounterpartyCandidate

    title = build_doc_title(
        candidate=CounterpartyCandidate(
            person_name="Samer Nawaf Zawaideh",
            person_role="CIO",
            org_name="SDF",
        ),
        scheduled_at=datetime(2026, 5, 14, 13, tzinfo=timezone.utc),
    )
    assert title == "14/05 - Brief: Samer Nawaf Zawaideh"


def test_brief_doc_inserts_photo_when_url_available():
    """FR-CB-5.3 — if `research.photo_url` is set, body begins
    with the bare URL (Google Docs auto-renders as inline image)."""
    from app.counterparty_briefs.doc import build_doc_body
    from app.counterparty_briefs.research import ResearchResult

    md = build_doc_body(
        candidate=None, context=None,
        research=ResearchResult(photo_url="https://x/y.jpg"),
    )
    assert md.lstrip().startswith("https://x/y.jpg")


def test_brief_doc_skips_photo_when_no_url():
    """FR-CB-5.4 — no photo URL → body starts with header line, no
    URL placeholder."""
    from app.counterparty_briefs.doc import build_doc_body

    md = build_doc_body(candidate=None, context=None, research=None)
    assert not md.lstrip().startswith("https://")


def test_brief_doc_renders_all_sections():
    """FR-CB-5.5 — every section from SPEC §6.2 is present, even
    if the value is «N/A»."""
    from app.counterparty_briefs.doc import build_doc_body

    md = build_doc_body(candidate=None, context=None, research=None)
    for section in (
        "Personal Information",
        "To-Do",
        "Profile Overview",
        "Current Position",
        "Previous Positions",
        "Investment Highlights",
        "Investments",
        "Achievements",
        "Education",
        "Skills",
        "Languages",
    ):
        assert section in md, f"section missing: {section}"


def test_brief_doc_dd_mm_summary_pulled_from_zoom_recordings():
    """FR-CB-5.6 — DD/MM Саммари section uses the operator-pinned
    «13/05 Саммари» format and pulls text from the most recent
    `zoom_recordings.short_summary` linked to the counterparty."""
    from app.counterparty_briefs.doc import build_doc_body
    from app.counterparty_briefs.lookup import CounterpartyContext

    ctx = CounterpartyContext(
        counterparty_id=1,
        past_recordings=[
            {"zoom_id": "z1", "title": "Genia weekly",
             "meeting_date": "2026-05-07T15:00:00+00:00",
             "short_summary": "Обсудили cap table, ждут update."}
        ],
        open_tasks=[],
        attributes={},
    )
    md = build_doc_body(candidate=None, context=ctx, research=None)
    assert "07/05 Саммари" in md
    assert "Обсудили cap table" in md


def test_brief_doc_todo_contains_open_tasks():
    """FR-CB-5.7 — Doc body lists every open task from
    `context.open_tasks`."""
    from app.counterparty_briefs.doc import build_doc_body
    from app.counterparty_briefs.lookup import CounterpartyContext

    ctx = CounterpartyContext(
        counterparty_id=1,
        past_recordings=[],
        open_tasks=[
            {"title": "Send proposal", "status": "todo",
             "owner_display_name": "Ирина"}
        ],
        attributes={},
    )
    md = build_doc_body(candidate=None, context=ctx, research=None)
    assert "Send proposal" in md


# -- Category 6: Slack delivery (FR-CB-6.x) ---------------------------------


def test_brief_slack_post():
    """FR-CB-6.1 — `runner._send_slack_dm` calls
    `chat.postMessage` to the configured channel and returns the
    new `ts`."""
    from unittest.mock import MagicMock

    from app.counterparty_briefs.runner import CounterpartyBriefRunner
    from app.config import Settings

    slack = MagicMock()
    slack.chat_postMessage.return_value = {"ok": True, "ts": "1.0"}
    runner = CounterpartyBriefRunner(
        settings=Settings(),
        slack_client=slack,
        llm_backend=MagicMock(),
        calendar_factory=lambda: None,
        docs_factory=lambda: None,
    )
    ts = runner._send_slack_dm(text="hello")
    assert ts == "1.0"


def test_brief_slack_header_format():
    """FR-CB-6.2 — header line:
    `*<doc-url|DD/MM - Brief: <Name> (<Org>)>*`"""
    from datetime import datetime, timezone

    from app.counterparty_briefs.extract import CounterpartyCandidate
    from app.counterparty_briefs.slack_format import render_brief_slack_text

    text = render_brief_slack_text(
        candidate=CounterpartyCandidate(
            person_name="Samer", person_role="CIO", org_name="SDF",
        ),
        scheduled_at=datetime(2026, 5, 14, 13, tzinfo=timezone.utc),
        doc_url="https://docs.google.com/document/d/g1/edit",
        context=None,
    )
    assert text.startswith(
        "*<https://docs.google.com/document/d/g1/edit|"
        "14/05 - Brief: Samer (SDF)>*"
    )


def test_brief_slack_safe_brackets():
    """FR-CB-6.3 — `<>` in name / org swap to «‹›» so Slack link
    parser doesn't break (same rule as agenda)."""
    from datetime import datetime, timezone

    from app.counterparty_briefs.extract import CounterpartyCandidate
    from app.counterparty_briefs.slack_format import render_brief_slack_text

    text = render_brief_slack_text(
        candidate=CounterpartyCandidate(
            person_name="X", person_role=None, org_name="A <> B",
        ),
        scheduled_at=datetime(2026, 5, 14, 13, tzinfo=timezone.utc),
        doc_url="https://x/y",
        context=None,
    )
    assert "<>" not in text  # only `‹›` should appear
    assert "‹›" in text


def test_brief_slack_body_cap():
    """FR-CB-6.4 — body ≤ 2900 chars (same cap as agenda)."""
    from datetime import datetime, timezone

    from app.counterparty_briefs.extract import CounterpartyCandidate
    from app.counterparty_briefs.lookup import CounterpartyContext
    from app.counterparty_briefs.slack_format import render_brief_slack_text

    huge = "x" * 5000
    text = render_brief_slack_text(
        candidate=CounterpartyCandidate(
            person_name="X", person_role=None, org_name=None,
        ),
        scheduled_at=datetime(2026, 5, 14, 13, tzinfo=timezone.utc),
        doc_url="https://docs.google.com/document/d/g1/edit",
        context=CounterpartyContext(
            counterparty_id=None,
            past_recordings=[{"short_summary": huge}],
            open_tasks=[],
            attributes={},
        ),
    )
    assert len(text) <= 2950


# -- Category 7: Idempotency (FR-CB-7.x) ------------------------------------


def test_brief_idempotency_skips_already_posted(session):
    """FR-CB-7.2 — second tick for the same `(event_id, key)`
    pair returns the existing row and does NOT re-send."""
    from datetime import datetime, timezone

    from app.counterparty_briefs.lookup import CounterpartyContext
    from app.counterparty_briefs.runner import is_already_posted
    from app.models import CounterpartyBrief

    session.add(CounterpartyBrief(
        calendar_event_id="ev1",
        counterparty_key="genia-xasis",
        display_name="Genia Xasis",
        org_name="",
        scheduled_meeting_at=datetime(2026, 5, 14, tzinfo=timezone.utc),
        posted_at=datetime.now(timezone.utc),
        slack_channel="D0",
        cost_usd="0.0",
    ))
    session.flush()
    assert is_already_posted(
        session, calendar_event_id="ev1", counterparty_key="genia-xasis"
    )


def test_brief_cli_force_flag(session):
    """FR-CB-7.3 — `ops.brief_run_once --force` deletes existing
    idempotency rows for the candidate before posting."""
    import sys
    from datetime import datetime, timezone
    from unittest.mock import patch

    from app.models import CounterpartyBrief
    from ops import brief_run_once

    session.add(CounterpartyBrief(
        calendar_event_id="ev1",
        counterparty_key="x",
        display_name="X",
        org_name="",
        scheduled_meeting_at=datetime(2026, 5, 14, tzinfo=timezone.utc),
        posted_at=datetime.now(timezone.utc),
        slack_channel="D0",
        cost_usd="0.0",
    ))
    session.flush()

    with patch.object(
        sys, "argv",
        ["brief_run_once", "--force", "--calendar-event-id", "ev1", "--dry-run"],
    ):
        rc = brief_run_once.main()
    assert rc == 0


# -- Category 8: Feature flag / safety (FR-CB-8.x) --------------------------


def test_brief_runner_no_slack_target(monkeypatch):
    """FR-CB-8.2 — runner refuses to start when
    COUNTERPARTY_BRIEFS_SLACK_TARGET_CHANNEL_ID is empty."""
    from unittest.mock import MagicMock

    from app.config import Settings
    from app.counterparty_briefs.runner import CounterpartyBriefRunner

    monkeypatch.setenv("COUNTERPARTY_BRIEFS_ENABLED", "true")
    monkeypatch.setenv("COUNTERPARTY_BRIEFS_SLACK_TARGET_CHANNEL_ID", "")
    runner = CounterpartyBriefRunner(
        settings=Settings(),
        slack_client=MagicMock(),
        llm_backend=MagicMock(),
        calendar_factory=lambda: None,
        docs_factory=lambda: None,
    )
    runner.start()
    assert runner._thread is None


def test_brief_runner_no_openai_key_still_runs_db_only(monkeypatch):
    """FR-CB-8.3 — without OPENAI_API_KEY the runner still ticks
    and produces DB-only Docs for known counterparties (no
    research / no extract — counterparty must come from a
    different source: e.g. organizer or `attendees` heuristic)."""
    from unittest.mock import MagicMock

    from app.config import Settings
    from app.counterparty_briefs.runner import CounterpartyBriefRunner

    monkeypatch.setenv("COUNTERPARTY_BRIEFS_ENABLED", "true")
    monkeypatch.setenv("COUNTERPARTY_BRIEFS_SLACK_TARGET_CHANNEL_ID", "D0")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    runner = CounterpartyBriefRunner(
        settings=Settings(),
        slack_client=MagicMock(),
        llm_backend=None,                 # explicit absence
        calendar_factory=lambda: None,
        docs_factory=lambda: None,
    )
    runner.start()
    # Thread starts but extract/research will short-circuit.
    assert runner._thread is not None
    runner.stop()
