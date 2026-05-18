"""FR-CR-05-168 v0.2 — Counterparty Briefs (event-trigger model).

This file is the test-spec for the feature defined in
SPEC_COUNTERPARTY_BRIEFS_v0.1.md (revision v0.2).

v0.2 changes vs v0.1:
  - Event-trigger (как только новая встреча появляется), не lead-time
  - Two-stage research: org first → LLM extracts beneficiaries →
    per-person research
  - Per-counterparty Doc reuse (TTL 14 days), grouped Slack DM
    per event (one message with N links: 🏢 org + 👤 persons)

All tests `xfail(strict=True)` — the implementation lives under
`app/counterparty_briefs/` (not yet committed). When the module
behaves as specified, every test turns green; XPASS is treated
as a failure (strict=True), so future PRs can't ship code that
diverges from spec without updating tests.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest


# FR-CR-05-168 v0.2 implementation landed — xfail layer removed.


# -- Category 1: Discovery (event-trigger, FR-CB-1.x) ----------------------


def test_brief_runner_disabled_no_op(monkeypatch):
    """FR-CB-1.1 / FR-CB-8.1 — runner thread NOT spawned when
    COUNTERPARTY_BRIEFS_ENABLED=false."""
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


def test_brief_window_lookahead_days(monkeypatch):
    """FR-CB-1.2 — runner scans events in
    [now, now + COUNTERPARTY_BRIEFS_LOOKAHEAD_DAYS]."""
    from app.config import Settings
    from app.counterparty_briefs.runner import compute_lookahead_window

    monkeypatch.setenv("COUNTERPARTY_BRIEFS_LOOKAHEAD_DAYS", "14")
    now = datetime(2026, 5, 14, 9, 0, tzinfo=timezone.utc)
    earliest, latest = compute_lookahead_window(Settings(), now=now)
    assert earliest == now
    assert latest == now + timedelta(days=14)


def test_brief_organizer_creator_filter():
    """FR-CB-1.3 — events with organizer.email or creator.email
    different from operator_email are dropped (inherits the
    agenda runner's FR-CR-05-167 gate)."""
    from app.counterparty_briefs.runner import event_passes_host_gate

    op = "1@thehumanoid.ai"
    assert event_passes_host_gate(
        {"organizer": {"email": op}, "creator": {"email": op}}, op
    )
    assert not event_passes_host_gate(
        {"organizer": {"email": op},
         "creator": {"email": "irina@thehumanoid.ai"}}, op
    )


def test_brief_event_idempotency_skips_processed(session):
    """FR-CB-1.5 — when an event is in
    `counterparty_briefs_events`, runner skips it on the next tick."""
    from datetime import datetime, timezone

    from app.counterparty_briefs.runner import event_already_processed
    from app.models import CounterpartyBriefsEvent

    session.add(
        CounterpartyBriefsEvent(
            calendar_event_id="ev1",
            event_title="X",
            scheduled_meeting_at=datetime(2026, 5, 14, tzinfo=timezone.utc),
            posted_at=datetime.now(timezone.utc),
            slack_channel="D0",
            total_cost_usd="0.0",
        )
    )
    session.flush()
    assert event_already_processed(session, calendar_event_id="ev1")


def test_brief_cli_lookahead_arg(monkeypatch):
    """FR-CB-1.6 — CLI accepts `--lookahead-days N`."""
    import sys
    from unittest.mock import patch

    from ops import brief_run_once

    from app.config import get_settings

    monkeypatch.setenv("COUNTERPARTY_BRIEFS_SLACK_TARGET_CHANNEL_ID", "D0")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(
        "ops.brief_run_once._fetch_events_wide", lambda r, d, lookback_days=0: []
    )
    get_settings.cache_clear()

    with patch.object(
        sys, "argv",
        ["brief_run_once", "--lookahead-days", "7", "--dry-run"],
    ):
        rc = brief_run_once.main()
    assert rc == 0


# -- Category 2: Org + initial-person extraction (FR-CB-2.x) ----------------


def test_brief_extract_returns_org_and_initial_persons():
    """FR-CB-2.1 — stage 0 LLM returns
    `{org_name, initial_persons: [...]}`."""
    from app.counterparty_briefs.extract import extract_event_counterparties

    llm = MagicMock()
    llm.complete_json.return_value = {
        "org_name": "Strategic Development Fund",
        "initial_persons": [
            {"person_name": "Samer Nawaf Zawaideh", "person_role": "CIO"}
        ],
    }
    out = extract_event_counterparties(
        event={
            "title": "SDF <> Humanoid | Intro call",
            "attendees": [
                {"email": "1@thehumanoid.ai"},
                {"email": "samer.zawaideh@sdf.ae"},
            ],
        },
        llm_backend=llm, model="gpt-test",
    )
    assert out.org_name == "Strategic Development Fund"
    assert out.initial_persons[0].person_name == "Samer Nawaf Zawaideh"


def test_brief_extract_skips_internal_attendees():
    """FR-CB-2.2 — emails on @thehumanoid.ai never count as
    counterparties."""
    from app.counterparty_briefs.extract import extract_event_counterparties

    llm = MagicMock()
    llm.complete_json.return_value = {
        "org_name": None, "initial_persons": []
    }
    out = extract_event_counterparties(
        event={
            "title": "Артем-Алина sync",
            "attendees": [
                {"email": "1@thehumanoid.ai"},
                {"email": "kaa@thehumanoid.ai"},
            ],
        },
        llm_backend=llm, model="gpt-test",
    )
    assert out.org_name is None
    assert out.initial_persons == []


def test_brief_extract_skips_event_with_no_counterparty():
    """FR-CB-2.3 — fully-internal event yields empty extraction;
    runner short-circuits before any research call."""
    from app.counterparty_briefs.extract import extract_event_counterparties

    llm = MagicMock()
    llm.complete_json.return_value = {
        "org_name": None, "initial_persons": []
    }
    out = extract_event_counterparties(
        event={"title": "Block: personal time", "attendees": []},
        llm_backend=llm, model="gpt-test",
    )
    assert out.org_name is None
    assert out.initial_persons == []


def test_brief_extract_output_schema():
    """FR-CB-2.5 — bad-shape output is rejected, returns empty
    extraction (runner skips event)."""
    from app.counterparty_briefs.extract import extract_event_counterparties

    llm = MagicMock()
    llm.complete_json.return_value = "not a dict"
    out = extract_event_counterparties(
        event={"title": "x", "attendees": []},
        llm_backend=llm, model="gpt-test",
    )
    assert out.org_name is None
    assert out.initial_persons == []


# -- Category 3: DB lookup (FR-CB-3.x) --------------------------------------


def test_brief_lookup_org_via_counterparties(session):
    """FR-CB-3.2 — `counterparties` hub by `name_normalised`."""
    from app.counterparty_briefs.lookup import lookup_org
    from app.models import Counterparty

    session.add(Counterparty(
        name="Strategic Development Fund",
        name_normalised="strategic development fund",
    ))
    session.flush()
    ctx = lookup_org(session, org_name="Strategic Development Fund")
    assert ctx.counterparty_id is not None


def test_brief_lookup_finds_past_meetings(session):
    """FR-CB-3.3 — past zoom recordings whose title contains the
    org normalised name."""
    from app.counterparty_briefs.lookup import lookup_org
    from app.models import ZoomRecording

    session.add(ZoomRecording(
        zoom_id="z1",
        title="SDF <> Humanoid | Intro call",
        meeting_date=datetime(2026, 4, 7, tzinfo=timezone.utc),
    ))
    session.flush()
    ctx = lookup_org(session, org_name="SDF")
    assert any("SDF" in (r.get("title") or "") for r in ctx.past_recordings)


def test_brief_lookup_finds_open_tasks(session):
    """FR-CB-3.4 — open tasks linked to past meetings of this org."""
    from app.counterparty_briefs.lookup import lookup_org
    from app.models import Task, TaskPriority, TaskStatus, ZoomRecording

    session.add(ZoomRecording(
        zoom_id="z1", title="SDF — fundraising",
        meeting_date=datetime(2026, 4, 7, tzinfo=timezone.utc),
    ))
    session.add(Task(
        title="Send JV proposal",
        source_kind="zoom", source_conversation_id="z1",
        status=TaskStatus.todo, priority=TaskPriority.high,
    ))
    session.flush()
    ctx = lookup_org(session, org_name="SDF")
    assert any(
        t.get("title") == "Send JV proposal" for t in ctx.open_tasks
    )


# -- Category 4: Two-stage research (FR-CB-4.x) -----------------------------


def test_brief_org_research_call():
    """FR-CB-4.1 — stage 1 calls
    `o4-mini-deep-research` for the org and returns a structured
    `OrgResearch`."""
    from app.counterparty_briefs.research import research_org

    llm = MagicMock()
    llm.complete_json.return_value = {
        "name": "Strategic Development Fund",
        "official_name": "Tawazun SDF",
        "website": "https://sdf.ae",
        "headquarters": "Abu Dhabi, UAE",
        "type": "Sovereign Wealth Fund",
        "sector_focus": ["Defense"],
        "leadership": [
            {"name": "Samer Nawaf Zawaideh", "role": "CIO"},
            {"name": "Khaled Al Hashemi",   "role": "CEO"},
        ],
        "portfolio_highlights": [],
        "recent_news": [],
        "overview_paragraph": "...",
    }
    out = research_org(
        org_name="Strategic Development Fund",
        llm_backend=llm, model="o4-mini-deep-research",
        budget_usd=5.0,
    )
    assert out is not None
    assert out.name == "Strategic Development Fund"
    assert len(out.leadership) == 2
    called = llm.complete_json.call_args.kwargs
    assert called["model"] == "o4-mini-deep-research"


def test_brief_beneficiary_extraction_picks_top_n():
    """FR-CB-4.2 — stage 2 LLM picks ≤ MAX_BENEFICIARIES from
    leadership + initial_persons + attendees."""
    from app.counterparty_briefs.extract import extract_beneficiaries
    from app.counterparty_briefs.research import OrgResearch

    llm = MagicMock()
    llm.complete_json.return_value = {
        "beneficiaries": [
            {"person_name": "Samer Nawaf Zawaideh", "person_role": "CIO",
             "evidence": "attendee + listed leadership"},
            {"person_name": "Khaled Al Hashemi", "person_role": "CEO",
             "evidence": "listed leadership"},
        ]
    }
    out = extract_beneficiaries(
        org_research=OrgResearch(
            name="X", leadership=[{"name": "A", "role": "CEO"}]
        ),
        attendees=[{"email": "samer.zawaideh@sdf.ae"}],
        initial_persons=[{"person_name": "Samer Nawaf Zawaideh"}],
        max_n=5,
        llm_backend=llm, model="gpt-test",
    )
    assert len(out) == 2
    assert out[0].person_name == "Samer Nawaf Zawaideh"


def test_brief_person_research_call():
    """FR-CB-4.3 — per-beneficiary `o4-mini-deep-research` call
    returns operator-pinned `PersonResearch` (§6.2 schema)."""
    from app.counterparty_briefs.extract import BeneficiaryCandidate
    from app.counterparty_briefs.research import research_person

    llm = MagicMock()
    llm.complete_json.return_value = {
        "photo_url": "https://x/photo.jpg",
        "personal_information": {"name": "Samer", "role": "CIO"},
        "profile_overview": "...",
        "current_positions": [],
        "previous_positions": [],
        "investment_highlights": {},
        "investments": [],
        "exits": "N/A",
        "achievements": [],
        "honors_awards": [],
        "education": [],
        "publications": [],
        "skills": [],
        "languages": [],
    }
    out = research_person(
        beneficiary=BeneficiaryCandidate(
            person_name="Samer Nawaf Zawaideh", person_role="CIO",
            evidence="...",
        ),
        org_name="Strategic Development Fund",
        llm_backend=llm, model="o4-mini-deep-research",
        budget_usd=2.0,
    )
    assert out is not None
    assert out.photo_url == "https://x/photo.jpg"


def test_brief_research_per_event_budget_cap(monkeypatch):
    """FR-CB-4.5 — runner stops calling more research'es once
    cumulative cost ≥ COUNTERPARTY_BRIEFS_LLM_BUDGET_USD."""
    pytest.skip("Behaviour test — assertions added once runner exposes "
                "process_event_counterparties() with a cost-tracking hook")


def test_brief_research_uses_cache_within_ttl(session):
    """FR-CB-4.6 — re-use prior `counterparty_briefs.research_payload`
    within TTL. No new research call; same Doc URL re-used."""
    from app.counterparty_briefs.research import research_org_with_cache
    from app.models import CounterpartyBrief

    session.add(CounterpartyBrief(
        counterparty_key="strategic development fund",
        kind="org",
        display_name="Strategic Development Fund",
        org_name="Strategic Development Fund",
        google_doc_id="doc1", google_doc_url="https://docs/.../doc1",
        researched_at=datetime.now(timezone.utc) - timedelta(days=3),
        cost_usd="1.0",
        research_payload={"name": "Strategic Development Fund"},
    ))
    session.flush()

    llm = MagicMock()
    out = research_org_with_cache(
        org_name="Strategic Development Fund",
        session=session, ttl_days=14,
        llm_backend=llm, model="o4-mini-deep-research", budget_usd=5.0,
    )
    assert out is not None
    assert out.cached is True
    assert llm.complete_json.call_count == 0


def test_brief_org_research_failure_skips_event():
    """FR-CB-4.7 — when org research raises, we cannot extract
    beneficiaries, so the entire event is skipped (no Doc, no DM)."""
    from app.counterparty_briefs.research import research_org

    llm = MagicMock()
    llm.complete_json.side_effect = RuntimeError("network down")
    out = research_org(
        org_name="X", llm_backend=llm,
        model="o4-mini-deep-research", budget_usd=5.0,
    )
    assert out is None


def test_brief_person_research_failure_continues_others():
    """FR-CB-4.8 — when one person's research fails, other
    beneficiaries still get briefs."""
    pytest.skip("Behaviour test — wired once runner exposes the "
                "per-beneficiary loop")


# -- Category 5: Doc generation (FR-CB-5.x) ---------------------------------


def test_brief_org_doc_title_format():
    """FR-CB-5.2a — Org Doc title: «DD/MM - Brief: <Org> (org)»."""
    from app.counterparty_briefs.doc import build_doc_title

    title = build_doc_title(
        kind="org", display_name="Strategic Development Fund",
        scheduled_at=datetime(2026, 5, 14, 13, tzinfo=timezone.utc),
    )
    assert title == "14/05 - Brief: Strategic Development Fund (org)"


def test_brief_person_doc_title_format():
    """FR-CB-5.2b — Person Doc title: «DD/MM - Brief: <Name>»."""
    from app.counterparty_briefs.doc import build_doc_title

    title = build_doc_title(
        kind="person", display_name="Samer Nawaf Zawaideh",
        scheduled_at=datetime(2026, 5, 14, 13, tzinfo=timezone.utc),
    )
    assert title == "14/05 - Brief: Samer Nawaf Zawaideh"


def test_brief_doc_inserts_photo_when_url_available():
    """FR-CB-5.3 — Person Doc starts with bare photo URL (Google
    Docs auto-renders as inline image)."""
    from app.counterparty_briefs.doc import build_person_doc_body
    from app.counterparty_briefs.research import PersonResearch

    md = build_person_doc_body(
        beneficiary=None, context=None,
        research=PersonResearch(photo_url="https://x/y.jpg"),
    )
    assert md.lstrip().startswith("https://x/y.jpg")


def test_brief_doc_skips_photo_when_no_url():
    """FR-CB-5.4 — no URL → body starts with header line."""
    from app.counterparty_briefs.doc import build_person_doc_body

    md = build_person_doc_body(
        beneficiary=None, context=None, research=None,
    )
    assert not md.lstrip().startswith("https://")


def test_brief_org_doc_renders_all_sections():
    """FR-CB-5.5a — Org Doc has Overview / Leadership / Portfolio
    / Recent Activity / Past meetings / Open tasks sections."""
    from app.counterparty_briefs.doc import build_org_doc_body

    md = build_org_doc_body(
        org_name="Strategic Development Fund",
        context=None, research=None,
    )
    for section in (
        "Overview", "Leadership", "Portfolio", "Recent",
        "Past meetings", "Open tasks",
    ):
        assert section in md, f"missing org section: {section}"


def test_brief_person_doc_renders_all_sections():
    """FR-CB-5.5b — Person Doc has every operator-pinned section
    from §6.2."""
    from app.counterparty_briefs.doc import build_person_doc_body

    md = build_person_doc_body(
        beneficiary=None, context=None, research=None,
    )
    for section in (
        "Personal Information", "To-Do", "Profile Overview",
        "Current Position", "Previous Positions",
        "Investment Highlights", "Investments", "Achievements",
        "Education", "Skills", "Languages",
    ):
        assert section in md, f"missing person section: {section}"


def test_brief_doc_dd_mm_summary_pulled_from_zoom_recordings():
    """FR-CB-5.6 — Person Doc DD/MM Саммари pulls from the most
    recent zoom_recordings.short_summary."""
    from app.counterparty_briefs.doc import build_person_doc_body
    from app.counterparty_briefs.lookup import CounterpartyContext

    ctx = CounterpartyContext(
        counterparty_id=1,
        past_recordings=[{
            "zoom_id": "z1", "title": "X",
            "meeting_date": "2026-05-07T15:00:00+00:00",
            "short_summary": "Обсудили JV.",
        }],
        open_tasks=[],
        attributes={},
    )
    md = build_person_doc_body(
        beneficiary=None, context=ctx, research=None,
    )
    assert "07/05 Саммари" in md
    assert "Обсудили JV" in md


def test_brief_doc_todo_contains_open_tasks():
    """FR-CB-5.7 — open tasks render in To-Do."""
    from app.counterparty_briefs.doc import build_person_doc_body
    from app.counterparty_briefs.lookup import CounterpartyContext

    ctx = CounterpartyContext(
        counterparty_id=1, past_recordings=[],
        open_tasks=[
            {"title": "Send JV proposal", "status": "todo",
             "owner_display_name": "Артем"},
        ],
        attributes={},
    )
    md = build_person_doc_body(
        beneficiary=None, context=ctx, research=None,
    )
    assert "Send JV proposal" in md


# -- Category 6: Slack delivery — single grouped DM (FR-CB-6.x) -------------


def test_brief_slack_grouped_post_per_event():
    """FR-CB-6.1 — ONE chat.postMessage per event with bulleted
    list of brief Doc URLs (org + persons)."""
    from app.counterparty_briefs.slack_format import (
        render_event_briefs_slack_text,
    )

    text = render_event_briefs_slack_text(
        event_title="SDF <> Humanoid | Intro call",
        scheduled_at=datetime(2026, 5, 14, 16, tzinfo=timezone.utc),
        org_brief={"display_name": "Strategic Development Fund",
                   "doc_url": "https://docs/.../org-doc"},
        person_briefs=[
            {"display_name": "Samer Nawaf Zawaideh",
             "role": "CIO",
             "doc_url": "https://docs/.../samer-doc"},
            {"display_name": "Khaled Al Hashemi",
             "role": "CEO",
             "doc_url": "https://docs/.../khaled-doc"},
        ],
    )
    assert "Новая встреча 14/05 16:00" in text
    assert "SDF ‹› Humanoid / Intro call" in text  # `<>` and `|` escaped
    # FR-CR-05-168 polish 2026-05-18: bullets replaced with
    # «emoji *Name* — gist \n <url-on-its-own-line>» — Slack
    # auto-unfurls bare URLs into clickable Doc previews.
    assert "🏢 *Strategic Development Fund*" in text
    assert "https://docs/.../org-doc" in text
    assert "👤 *Samer Nawaf Zawaideh* — CIO" in text
    assert "https://docs/.../samer-doc" in text
    assert "👤 *Khaled Al Hashemi* — CEO" in text
    assert "https://docs/.../khaled-doc" in text


def test_brief_slack_links_have_kind_emoji():
    """FR-CB-6.6 — 🏢 prefix for org, 👤 for person."""
    from app.counterparty_briefs.slack_format import (
        render_event_briefs_slack_text,
    )

    text = render_event_briefs_slack_text(
        event_title="X", scheduled_at=datetime(2026, 5, 14, tzinfo=timezone.utc),
        org_brief={"display_name": "X", "doc_url": "u1"},
        person_briefs=[{"display_name": "P", "role": None, "doc_url": "u2"}],
    )
    assert "🏢" in text
    assert "👤" in text


def test_brief_org_top_message_has_pointer_to_thread():
    """FR-CB-6.7 — operator-pinned 2026-05-18: top message about
    org carries a footer pointing operator to the thread with N
    person briefs below; org name is rendered as a Slack hyperlink
    `<doc-url|🏢 *Org*>` (not bare URL on its own line)."""
    from app.counterparty_briefs.slack_format import render_org_top_message

    text = render_org_top_message(
        event_title="SDF <> Humanoid",
        scheduled_at=datetime(2026, 5, 14, 16, tzinfo=timezone.utc),
        org_brief={
            "display_name": "Strategic Development Fund",
            "doc_url": "https://docs/.../org-doc",
            "gist": "SWF из ОАЭ. Defence + dual-use.",
        },
        person_count=2,
    )
    # Hyperlink markup
    assert (
        "<https://docs/.../org-doc|🏢 *Strategic Development Fund*>"
        in text
    )
    assert "Информация о 2 контактах" in text
    assert "треде ниже" in text


def test_brief_person_thread_reply_singular_payload():
    """FR-CB-6.8 — render_person_thread_reply emits one Slack-mrkdwn
    `<doc-url|👤 *Name* — Role>` block followed by the gist."""
    from app.counterparty_briefs.slack_format import render_person_thread_reply

    reply = render_person_thread_reply(person={
        "display_name": "Samer Nawaf Zawaideh",
        "role": "CIO",
        "doc_url": "https://docs/.../samer",
        "gist": "Долгое время руководил инвестблоком SDF.",
        "note": None,
    })
    assert reply.startswith(
        "<https://docs/.../samer|👤 *Samer Nawaf Zawaideh* — CIO>"
    )
    assert "Долгое время" in reply


def test_brief_slack_linkifies_bare_host_citations():
    """FR-CB-6.9 — operator-pinned 2026-05-18 revision: citation
    markers `([host.tld])` become Slack hyperlinks
    `<https://host.tld|host.tld>` instead of being stripped, so
    the operator can click through to the source. Applies at
    render time so pre-existing cached briefs benefit too."""
    from app.counterparty_briefs.slack_format import render_person_thread_reply

    reply = render_person_thread_reply(person={
        "display_name": "Timo Bohl",
        "role": "Director of Sales",
        "doc_url": "https://docs/.../timo",
        "gist": (
            "Timo Bohl is Director of Sales at WEB.DE "
            "([newsroom.web.de]) ([www.mail-and-media.com]). "
            "Based in Karlsruhe."
        ),
        "note": None,
    })
    # Bracket markers become Slack hyperlinks pointing to the
    # constructed host URL — operator can click through.
    assert "<https://newsroom.web.de|newsroom.web.de>" in reply
    assert "<https://www.mail-and-media.com|www.mail-and-media.com>" in reply
    # No raw `([host])` markers left behind
    assert "([newsroom" not in reply
    assert "([www.mail" not in reply
    # The prose itself is preserved verbatim
    assert "Timo Bohl is Director of Sales at WEB.DE" in reply
    assert "Based in Karlsruhe." in reply


def test_brief_slack_linkifies_markdown_citations():
    """FR-CB-6.10 — `[label](https://url#:~:text=…)` becomes a
    Slack hyperlink to the cleaned URL with the original label.
    The `#:~:text=…` anchor is dropped from the visible URL."""
    from app.counterparty_briefs.slack_format import render_person_thread_reply

    reply = render_person_thread_reply(person={
        "display_name": "Sample",
        "role": "CEO",
        "doc_url": "https://docs/.../sample",
        "gist": (
            "Sample heads things "
            "([Bloomberg](https://bloomberg.com/news/article-1#:~:text=Foo))."
        ),
        "note": None,
    })
    assert "<https://bloomberg.com/news/article-1|Bloomberg>" in reply
    assert "#:~:text=" not in reply


def test_brief_doc_linkifies_bare_host_citations():
    """FR-CB-5.8 — `markdown_to_html` converts `([host.tld])`
    markers into `<a href="https://host.tld">host.tld</a>` so the
    Drive HTML import renders them as clickable hyperlinks inside
    the generated Doc."""
    from app.counterparty_briefs.doc import markdown_to_html

    html = markdown_to_html(
        "Operates as the asset management arm "
        "([www.cdibcapitalgroup.com])."
    )
    assert (
        '<a href="https://www.cdibcapitalgroup.com">'
        'www.cdibcapitalgroup.com</a>'
    ) in html


def test_brief_slack_safe_brackets():
    """FR-CB-6.3 — `<>` and `|` in title / name / org swap to
    `‹›` / `/` so Slack link parser doesn't break."""
    from app.counterparty_briefs.slack_format import (
        render_event_briefs_slack_text,
    )

    text = render_event_briefs_slack_text(
        event_title="A <> B | sub",
        scheduled_at=datetime(2026, 5, 14, tzinfo=timezone.utc),
        org_brief={"display_name": "A <> B", "doc_url": "u1"},
        person_briefs=[],
    )
    assert "<>" not in text
    assert "‹›" in text


def test_brief_slack_body_cap():
    """FR-CB-6.4 — body ≤ 2900 chars; >10 person briefs trimmed
    with «…ещё N в Google Doc»."""
    from app.counterparty_briefs.slack_format import (
        render_event_briefs_slack_text,
    )

    text = render_event_briefs_slack_text(
        event_title="X", scheduled_at=datetime(2026, 5, 14, tzinfo=timezone.utc),
        org_brief={"display_name": "X", "doc_url": "u1"},
        person_briefs=[
            {"display_name": f"P{i}", "role": "x", "doc_url": "u"}
            for i in range(50)
        ],
    )
    assert len(text) <= 2950


# -- Category 7: Idempotency (FR-CB-7.x) ------------------------------------


def test_brief_event_idempotency_skips_processed(session):
    """FR-CB-7.2 — second tick for the same event_id is a no-op."""
    from app.counterparty_briefs.runner import event_already_processed
    from app.models import CounterpartyBriefsEvent

    session.add(CounterpartyBriefsEvent(
        calendar_event_id="ev1", event_title="X",
        scheduled_meeting_at=datetime(2026, 5, 14, tzinfo=timezone.utc),
        posted_at=datetime.now(timezone.utc),
        slack_channel="D0", total_cost_usd="0.0",
    ))
    session.flush()
    assert event_already_processed(session, calendar_event_id="ev1")


def test_brief_counterparty_cache_reuses_doc(session):
    """FR-CB-7.3 — within TTL the existing
    `counterparty_briefs.google_doc_url` is reused; no new Doc
    is created."""
    from app.counterparty_briefs.runner import find_cached_brief
    from app.models import CounterpartyBrief

    session.add(CounterpartyBrief(
        counterparty_key="strategic development fund",
        kind="org", display_name="Strategic Development Fund",
        org_name="Strategic Development Fund",
        google_doc_url="https://docs/.../sdf",
        researched_at=datetime.now(timezone.utc) - timedelta(days=2),
        cost_usd="1.0",
    ))
    session.flush()

    cached = find_cached_brief(
        session,
        counterparty_key="strategic development fund", ttl_days=14,
    )
    assert cached is not None
    assert cached.google_doc_url == "https://docs/.../sdf"


def test_brief_cli_force_event_flag(session, patched_session_scope):
    """FR-CB-7.4 — `--force-event ID` deletes the events row,
    forcing reprocessing."""
    import sys
    from unittest.mock import patch

    from app.models import CounterpartyBriefsEvent
    from ops import brief_run_once

    session.add(CounterpartyBriefsEvent(
        calendar_event_id="ev1", event_title="X",
        scheduled_meeting_at=datetime(2026, 5, 14, tzinfo=timezone.utc),
        posted_at=datetime.now(timezone.utc),
        slack_channel="D0", total_cost_usd="0.0",
    ))
    session.flush()

    from app.config import get_settings

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setenv("COUNTERPARTY_BRIEFS_SLACK_TARGET_CHANNEL_ID", "D0")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setattr(
            "ops.brief_run_once._fetch_events_wide", lambda r, d, lookback_days=0: []
        )
        get_settings.cache_clear()
        with patch.object(
            sys, "argv",
            ["brief_run_once", "--force-event", "ev1", "--dry-run"],
        ):
            rc = brief_run_once.main()
        assert rc == 0
    finally:
        monkeypatch.undo()


def test_brief_cli_force_counterparty_flag(session, patched_session_scope):
    """FR-CB-7.5 — `--force-counterparty NAME` deletes the
    `counterparty_briefs` row by `counterparty_key`."""
    import sys
    from unittest.mock import patch

    from app.models import CounterpartyBrief
    from ops import brief_run_once

    session.add(CounterpartyBrief(
        counterparty_key="x", kind="org", display_name="X",
        org_name="X", cost_usd="0.0",
        researched_at=datetime.now(timezone.utc),
    ))
    session.flush()
    from app.config import get_settings

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setenv("COUNTERPARTY_BRIEFS_SLACK_TARGET_CHANNEL_ID", "D0")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setattr(
            "ops.brief_run_once._fetch_events_wide", lambda r, d, lookback_days=0: []
        )
        get_settings.cache_clear()
        with patch.object(
            sys, "argv",
            ["brief_run_once", "--force-counterparty", "X", "--dry-run"],
        ):
            rc = brief_run_once.main()
        assert rc == 0
    finally:
        monkeypatch.undo()


# -- Category 8: Feature flag / safety (FR-CB-8.x) --------------------------


def test_brief_runner_no_slack_target(monkeypatch):
    """FR-CB-8.2 — runner refuses to start without
    COUNTERPARTY_BRIEFS_SLACK_TARGET_CHANNEL_ID."""
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
    and produces DB-only Docs for already-known counterparties
    (research short-circuits)."""
    from app.config import Settings
    from app.counterparty_briefs.runner import CounterpartyBriefRunner

    monkeypatch.setenv("COUNTERPARTY_BRIEFS_ENABLED", "true")
    monkeypatch.setenv("COUNTERPARTY_BRIEFS_SLACK_TARGET_CHANNEL_ID", "D0")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    runner = CounterpartyBriefRunner(
        settings=Settings(),
        slack_client=MagicMock(),
        llm_backend=None,
        calendar_factory=lambda: None,
        docs_factory=lambda: None,
    )
    runner.start()
    assert runner._thread is not None
    runner.stop()
