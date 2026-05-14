"""FR-CR-05-165 — Pre-meeting agenda: unit tests.

Covers the pure-logic surface:
  - title normalisation rules
  - prior-recording matching by normalised title
  - open-tasks filtering by zoom_id + status
  - candidate building (dedup, min_prior_meetings)
  - idempotency persistence
  - Slack message rendering

LLM call + Calendar / Slack APIs are NOT exercised here — those
live in `compose.py` and `runner.py` and need fakes. We test the
LLM output coercion + Slack formatter via fake objects.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.agenda.compose import AgendaOutput, compose_agenda
from app.agenda.service import (
    AgendaCandidate,
    AgendaService,
    build_candidates,
    find_prior_recordings,
    normalise_title,
    open_tasks_for_recordings,
)
from app.agenda.slack_format import render_agenda_text
from app.models import MeetingAgenda, Task, TaskPriority, TaskStatus, ZoomRecording


# -- normalise_title -----------------------------------------------------------


def test_normalise_title_lowercases_collapses_ws_and_drops_punct():
    assert normalise_title("Genia Xasis <> Humanoid (Weekly sync)") == \
        "genia xasis humanoid weekly sync"


def test_normalise_title_handles_cyrillic_and_eyo():
    assert normalise_title("Лётучка — Подземелья") == "летучка подземелья"


def test_normalise_title_empty_and_none():
    assert normalise_title(None) == ""
    assert normalise_title("") == ""
    assert normalise_title("   ") == ""


def test_normalise_title_is_stable_across_minor_variants():
    a = normalise_title("Wellness Holding <> Humanoid")
    b = normalise_title("wellness  holding <>  humanoid")
    c = normalise_title("Wellness Holding<>Humanoid")
    assert a == b == c


# -- find_prior_recordings -----------------------------------------------------


def test_find_prior_recordings_matches_normalised_title(session):
    now = datetime(2026, 5, 13, 14, 0, 0, tzinfo=timezone.utc)
    session.add_all(
        [
            ZoomRecording(
                zoom_id="z1",
                title="Genia Xasis <> Humanoid (Weekly fundraising sync)",
                meeting_date=now - timedelta(days=7),
            ),
            ZoomRecording(
                zoom_id="z2",
                title="genia xasis  <>  humanoid (weekly fundraising sync)",
                meeting_date=now - timedelta(days=14),
            ),
            # Different title — must NOT match.
            ZoomRecording(
                zoom_id="z3",
                title="Wellness Holding <> Humanoid",
                meeting_date=now - timedelta(days=7),
            ),
        ]
    )
    session.flush()

    rows = find_prior_recordings(
        session,
        title="Genia Xasis <> Humanoid (Weekly fundraising sync)",
        lookback_days=30,
        now=now,
    )
    zoom_ids = [r.zoom_id for r in rows]
    assert set(zoom_ids) == {"z1", "z2"}
    assert "z3" not in zoom_ids
    # Newest first.
    assert zoom_ids[0] == "z1"


def test_find_prior_recordings_respects_lookback_cutoff(session):
    now = datetime(2026, 5, 13, 14, 0, 0, tzinfo=timezone.utc)
    session.add_all(
        [
            ZoomRecording(
                zoom_id="z_old",
                title="Weekly sync",
                meeting_date=now - timedelta(days=120),
            ),
            ZoomRecording(
                zoom_id="z_recent",
                title="Weekly sync",
                meeting_date=now - timedelta(days=7),
            ),
        ]
    )
    session.flush()

    rows = find_prior_recordings(
        session, title="Weekly sync", lookback_days=30, now=now
    )
    ids = [r.zoom_id for r in rows]
    assert ids == ["z_recent"]


def test_find_prior_recordings_skips_future_scheduled(session):
    """A future ZoomRecording with the same title shouldn't be
    treated as «prior» — that's the upcoming instance itself."""
    now = datetime(2026, 5, 13, 14, 0, 0, tzinfo=timezone.utc)
    session.add_all(
        [
            ZoomRecording(
                zoom_id="z_future",
                title="Weekly sync",
                meeting_date=now + timedelta(hours=1),
            ),
            ZoomRecording(
                zoom_id="z_past",
                title="Weekly sync",
                meeting_date=now - timedelta(days=7),
            ),
        ]
    )
    session.flush()

    rows = find_prior_recordings(
        session, title="Weekly sync", lookback_days=30, now=now
    )
    assert [r.zoom_id for r in rows] == ["z_past"]


# -- open_tasks_for_recordings -------------------------------------------------


def test_open_tasks_for_recordings_filters_status_and_orders_by_priority(session):
    """Done dropped (only TaskStatus.done is a terminal state in
    current schema); remaining sorted urgent → high → medium →
    low, then due asc.

    FR-CR-05-165: cancelled / blocked do NOT exist in this
    project's TaskStatus enum yet. If they're added later, update
    the agenda filter AND this test together."""
    session.add_all(
        [
            # Should appear:
            Task(
                title="Urgent open",
                source_kind="zoom",
                source_conversation_id="z1",
                status=TaskStatus.todo,
                priority=TaskPriority.urgent,
            ),
            Task(
                title="High in_progress",
                source_kind="zoom",
                source_conversation_id="z2",
                status=TaskStatus.in_progress,
                priority=TaskPriority.high,
            ),
            # Filtered: done
            Task(
                title="Done",
                source_kind="zoom",
                source_conversation_id="z1",
                status=TaskStatus.done,
                priority=TaskPriority.high,
            ),
            # Filtered: not a zoom source
            Task(
                title="Slack task same id",
                source_kind="slack",
                source_conversation_id="z1",
                status=TaskStatus.todo,
                priority=TaskPriority.high,
            ),
            # Filtered: different zoom_id
            Task(
                title="Other zoom",
                source_kind="zoom",
                source_conversation_id="z_other",
                status=TaskStatus.todo,
                priority=TaskPriority.high,
            ),
        ]
    )
    session.flush()

    rows = open_tasks_for_recordings(session, zoom_ids=["z1", "z2"])
    titles = [r.title for r in rows]
    # Urgent comes before High.
    assert titles == ["Urgent open", "High in_progress"]


def test_open_tasks_for_recordings_empty_list_returns_empty(session):
    assert open_tasks_for_recordings(session, zoom_ids=[]) == []


# -- build_candidates ----------------------------------------------------------


def _evt(id_: str, title: str, when: datetime, **extras):
    return {"id": id_, "title": title, "start": when, **extras}


def test_build_candidates_drops_first_time_meetings(session):
    """Event with NO prior recordings (= first time the title is
    seen) must NOT yield a candidate; it's not «recurring yet»."""
    now = datetime(2026, 5, 13, 14, 0, 0, tzinfo=timezone.utc)
    # Empty DB → no priors.
    events = [_evt("ev1", "Brand new sync", now + timedelta(minutes=10))]

    candidates = build_candidates(
        session, events=events, lookback_days=30,
        min_prior_meetings=1, now=now,
    )
    assert candidates == []


def test_build_candidates_keeps_recurring_meeting(session):
    now = datetime(2026, 5, 13, 14, 0, 0, tzinfo=timezone.utc)
    session.add(
        ZoomRecording(
            zoom_id="z_prev",
            title="Weekly sync",
            meeting_date=now - timedelta(days=7),
        )
    )
    session.flush()

    events = [_evt("ev_x", "Weekly sync", now + timedelta(minutes=10))]
    candidates = build_candidates(
        session, events=events, lookback_days=30,
        min_prior_meetings=1, now=now,
    )
    assert len(candidates) == 1
    c = candidates[0]
    assert c.calendar_event_id == "ev_x"
    assert c.title == "Weekly sync"
    assert c.scheduled_start_at == now + timedelta(minutes=10)
    assert len(c.prior_recordings) == 1
    assert c.prior_recordings[0]["zoom_id"] == "z_prev"


def test_build_candidates_skips_already_posted(session):
    now = datetime(2026, 5, 13, 14, 0, 0, tzinfo=timezone.utc)
    session.add(
        ZoomRecording(
            zoom_id="z_prev",
            title="Weekly sync",
            meeting_date=now - timedelta(days=7),
        )
    )
    # Already-posted row for the same calendar_event_id.
    session.add(
        MeetingAgenda(
            calendar_event_id="ev_posted",
            title="Weekly sync",
            title_normalised=normalise_title("Weekly sync"),
            scheduled_start_at=now + timedelta(minutes=10),
            posted_at=now - timedelta(minutes=1),
            slack_channel="D0",
        )
    )
    session.flush()

    events = [_evt("ev_posted", "Weekly sync", now + timedelta(minutes=10))]
    candidates = build_candidates(
        session, events=events, lookback_days=30,
        min_prior_meetings=1, now=now,
    )
    assert candidates == []


def test_build_candidates_respects_min_prior_meetings(session):
    """min_prior_meetings=2 means we need at least TWO prior
    recordings before treating the event as recurring."""
    now = datetime(2026, 5, 13, 14, 0, 0, tzinfo=timezone.utc)
    session.add(
        ZoomRecording(
            zoom_id="z_only", title="Weekly sync",
            meeting_date=now - timedelta(days=7),
        )
    )
    session.flush()

    events = [_evt("ev1", "Weekly sync", now + timedelta(minutes=10))]
    # Only 1 prior → with threshold 2, no candidates.
    assert build_candidates(
        session, events=events, lookback_days=30,
        min_prior_meetings=2, now=now,
    ) == []


# -- AgendaService idempotency -------------------------------------------------


def test_agenda_service_is_already_posted_and_record_post(session):
    now = datetime(2026, 5, 13, 14, 0, 0, tzinfo=timezone.utc)
    candidate = AgendaCandidate(
        calendar_event_id="ev1",
        recurring_event_id="rec_root",
        title="Weekly sync",
        title_normalised="weekly sync",
        scheduled_start_at=now + timedelta(minutes=10),
    )
    svc = AgendaService()
    assert svc.is_already_posted(session, calendar_event_id="ev1") is False
    svc.record_post(
        session, candidate=candidate, slack_channel="D0",
        slack_ts="111.222", google_doc_id="g1",
        google_doc_url="https://docs.google.com/d/g1",
        prior_zoom_ids=["z_prev"],
    )
    session.flush()
    assert svc.is_already_posted(session, calendar_event_id="ev1") is True


# -- compose_agenda (LLM stub) -------------------------------------------------


def test_compose_build_user_prompt_handles_attendees_as_dicts():
    """FR-CR-05-167 hotfix: `_build_user_prompt` previously did
    `", ".join(candidate.attendees)`, which crashed on Calendar
    API's `[{email, displayName, ...}]` shape. Must accept both
    string and dict items."""
    from app.agenda.compose import _build_user_prompt

    candidate = AgendaCandidate(
        calendar_event_id="ev1",
        recurring_event_id=None,
        title="Mixed attendees",
        title_normalised="mixed attendees",
        scheduled_start_at=datetime(2026, 5, 14, 11, tzinfo=timezone.utc),
        attendees=[
            "Artem Sokolov",
            {"displayName": "Irina Shipilova", "email": "irina@x"},
            {"email": "third@x"},
        ],
    )
    prompt = _build_user_prompt(candidate)
    # Should not crash + names should appear correctly.
    assert "Artem Sokolov" in prompt
    assert "Irina Shipilova" in prompt
    assert "third@x" in prompt


def test_compose_agenda_happy_path():
    """LLM returns a well-formed dict — `compose_agenda` wraps it
    in an AgendaOutput dataclass."""
    candidate = AgendaCandidate(
        calendar_event_id="ev1",
        recurring_event_id=None,
        title="Weekly sync",
        title_normalised="weekly sync",
        scheduled_start_at=datetime(2026, 5, 13, 14, tzinfo=timezone.utc),
    )
    llm = MagicMock()
    llm.complete_json.return_value = {
        "previous_recap": ["обсудили roadmap", "договорились про deck"],
        "tasks_checklist": [
            {"task_id": 1, "title": "Прислать deck", "status": "todo",
             "owner": "admin", "due": "2026-05-15"},
        ],
        "open_questions": ["согласовать timing pre-seed"],
        "doc_body_md": "## Из прошлого раза\n- обсудили roadmap\n",
    }
    out = compose_agenda(candidate, llm_backend=llm, model="gpt-test")
    assert isinstance(out, AgendaOutput)
    assert out.previous_recap == ["обсудили roadmap", "договорились про deck"]
    assert len(out.tasks_checklist) == 1
    assert out.open_questions == ["согласовать timing pre-seed"]
    assert "Из прошлого раза" in out.doc_body_md


def test_compose_agenda_returns_none_on_llm_exception():
    candidate = AgendaCandidate(
        calendar_event_id="ev1",
        recurring_event_id=None,
        title="t",
        title_normalised="t",
        scheduled_start_at=datetime(2026, 5, 13, tzinfo=timezone.utc),
    )
    llm = MagicMock()
    llm.complete_json.side_effect = RuntimeError("network down")
    assert compose_agenda(candidate, llm_backend=llm, model="m") is None


def test_compose_agenda_returns_none_on_non_dict_output():
    candidate = AgendaCandidate(
        calendar_event_id="ev1",
        recurring_event_id=None,
        title="t",
        title_normalised="t",
        scheduled_start_at=datetime(2026, 5, 13, tzinfo=timezone.utc),
    )
    llm = MagicMock()
    llm.complete_json.return_value = "not a dict"
    assert compose_agenda(candidate, llm_backend=llm, model="m") is None


# -- render_agenda_text --------------------------------------------------------


def test_render_agenda_text_format_matches_operator_pin():
    """FR-CR-05-167 operator-pinned 2026-05-14: повестка в стиле
    post-meeting summary. Без эмодзи. Хедер «DD/MM - <title> -
    Повестка». Recap абзацем «На прошлой встрече: ...». Задачи
    нумерованным списком «1) title - description — owner • DD.MM.YYYY»."""
    candidate = AgendaCandidate(
        calendar_event_id="ev1",
        recurring_event_id=None,
        title="Fundraising daily",
        title_normalised="fundraising daily",
        scheduled_start_at=datetime(2026, 5, 14, 11, tzinfo=timezone.utc),
        attendees=["Artem Sokolov", "Irina Shipilova"],
    )
    output = AgendaOutput(
        previous_recap=[
            "Разобрали список инвесторов к AIM Summit",
            "Зафиксировали приоритетные контакты",
        ],
        tasks_checklist=[
            {
                "task_id": 1,
                "title": "Bracket Capital",
                "description": "отправить аутрич как релевантному фонду",
                "status": "todo",
                "owner": "Irina Shipilova",
                "due": "2026-05-13",
            },
            {
                "task_id": 2,
                "title": "Cambridge Source",
                "description": "follow-up по интро",
                "status": "in_progress",
                "owner": "Irina Shipilova",
                "due": "2026-05-14",
            },
        ],
        open_questions=["Утром начать outreach"],
        doc_body_md="…",
    )
    text = render_agenda_text(
        candidate=candidate, output=output,
        doc_url="https://docs.google.com/document/d/g1/edit",
    )
    # Header
    assert text.startswith("14/05 - Fundraising daily - Повестка")
    # No emojis
    for em in ("📋", "✅", "🎯", "📄", "☐", "☑", "▣", "⛔", "✕"):
        assert em not in text, f"emoji {em} must not appear in agenda"
    # Attendees section
    assert "Участники: Artem Sokolov, Irina Shipilova" in text
    # Recap as prose paragraph
    assert "На прошлой встрече: " in text
    assert "Разобрали список инвесторов" in text
    # Numbered task list
    assert "К обсуждению:" in text
    assert "1) Bracket Capital - отправить аутрич" in text
    assert "— Irina Shipilova" in text
    assert "13.05.2026" in text
    # in_progress status surfaces as a [..] suffix
    assert "[in_progress]" in text
    # Open question is appended as a continuation item
    assert "Утром начать outreach" in text
    # Doc link (no emoji prefix)
    assert "Подробно: https://docs.google.com/document/d/g1/edit" in text


def test_render_agenda_text_truncates_when_too_long():
    candidate = AgendaCandidate(
        calendar_event_id="ev1",
        recurring_event_id=None,
        title="Weekly sync",
        title_normalised="weekly sync",
        scheduled_start_at=datetime(2026, 5, 13, 15, tzinfo=timezone.utc),
    )
    output = AgendaOutput(
        previous_recap=["x" * 400] * 5,
        tasks_checklist=[
            {"title": "y" * 100, "description": "z" * 200,
             "status": "todo", "owner": "a"}
        ] * 30,
        open_questions=["w" * 200] * 4,
        doc_body_md="…",
    )
    text = render_agenda_text(
        candidate=candidate, output=output,
        doc_url="https://docs.google.com/d/x",
    )
    assert len(text) <= 2950
    assert "Подробно" in text  # doc link mention survives the trim


def test_render_agenda_text_omits_doc_section_when_no_url():
    candidate = AgendaCandidate(
        calendar_event_id="ev1",
        recurring_event_id=None,
        title="Weekly sync",
        title_normalised="weekly sync",
        scheduled_start_at=datetime(2026, 5, 13, tzinfo=timezone.utc),
    )
    output = AgendaOutput(
        previous_recap=["a"], tasks_checklist=[], open_questions=["b"],
        doc_body_md="",
    )
    text = render_agenda_text(
        candidate=candidate, output=output, doc_url=None,
    )
    assert "Подробно" not in text


def test_render_agenda_text_attendees_accepts_dict_or_string():
    """FR-CR-05-167 bug-fix 2026-05-14: Calendar API returns
    attendees as `[{email, displayName, ...}]` while the Apps
    Script proxy emits plain strings. Slack renderer must accept
    both without crashing on `", ".join` of dicts."""
    candidate = AgendaCandidate(
        calendar_event_id="ev1",
        recurring_event_id=None,
        title="Mixed",
        title_normalised="mixed",
        scheduled_start_at=datetime(2026, 5, 14, 11, tzinfo=timezone.utc),
        attendees=[
            "Artem Sokolov",
            {"displayName": "Irina Shipilova", "email": "irina@x"},
            {"email": "third@x"},  # no displayName — falls back to email
            {"foo": "bar"},  # garbage — silently dropped
        ],
    )
    output = AgendaOutput(
        previous_recap=[], tasks_checklist=[], open_questions=[],
        doc_body_md="",
    )
    text = render_agenda_text(
        candidate=candidate, output=output, doc_url=None,
    )
    assert "Участники: Artem Sokolov, Irina Shipilova, third@x" in text


def test_render_agenda_text_done_tasks_show_status_label():
    """FR-CR-05-167: done / cancelled tasks should still appear in
    «К обсуждению» (operator wants the full status snapshot), with
    a `[done]` / `[cancelled]` suffix marking them."""
    candidate = AgendaCandidate(
        calendar_event_id="ev1",
        recurring_event_id=None,
        title="Weekly sync",
        title_normalised="weekly sync",
        scheduled_start_at=datetime(2026, 5, 13, tzinfo=timezone.utc),
    )
    output = AgendaOutput(
        previous_recap=[],
        tasks_checklist=[
            {"title": "Sent deck", "status": "done", "owner": "admin"},
            {"title": "Drop investor X", "status": "cancelled", "owner": "admin"},
            {"title": "Reach out Y", "status": "todo", "owner": "admin"},
        ],
        open_questions=[],
        doc_body_md="",
    )
    text = render_agenda_text(
        candidate=candidate, output=output, doc_url=None,
    )
    assert "1) Sent deck — admin • [done]" in text
    assert "2) Drop investor X — admin • [cancelled]" in text
    # todo gets no status suffix (operator's default for the list)
    assert "3) Reach out Y — admin" in text
    assert "[todo]" not in text


# -- runner no-op safety -------------------------------------------------------


def test_runner_disabled_no_op(monkeypatch):
    """Sanity: when AGENDA_ENABLED=false, calling .start() returns
    without spinning the thread (no Calendar / Slack / LLM I/O)."""
    from app.agenda.runner import AgendaRunner
    from app.config import Settings

    s = Settings(
        agenda_enabled=False,
        agenda_slack_target_channel_id="D0",
    )
    runner = AgendaRunner(
        settings=s,
        slack_client=MagicMock(),
        llm_backend=MagicMock(),
        calendar_factory=lambda: None,
        docs_factory=lambda: None,
    )
    runner.start()
    # Thread was never created.
    assert runner._thread is None


def test_runner_exits_when_no_calendar_source(monkeypatch):
    """FR-CR-05-165: when AGENDA_ENABLED=true but neither
    GOOGLE_CALENDAR_CLIENT_ID nor CALENDAR_APPS_SCRIPT_URL is set,
    runner must NOT start the thread (no idle ticks against
    nothing)."""
    from app.agenda.runner import AgendaRunner
    from app.config import Settings

    monkeypatch.setenv("AGENDA_ENABLED", "true")
    monkeypatch.setenv("AGENDA_SLACK_TARGET_CHANNEL_ID", "D0")
    monkeypatch.setenv("CALENDAR_APPS_SCRIPT_URL", "")

    runner = AgendaRunner(
        settings=Settings(),
        slack_client=MagicMock(),
        llm_backend=MagicMock(),
        calendar_factory=None,
        docs_factory=lambda: None,
    )
    runner.start()
    assert runner._thread is None


def test_runner_starts_with_apps_script_only(monkeypatch):
    """FR-CR-05-165: Apps Script proxy is a valid calendar source —
    runner must NOT bail when calendar_factory is None as long as
    CALENDAR_APPS_SCRIPT_URL is set."""
    from app.agenda.runner import AgendaRunner
    from app.config import Settings

    monkeypatch.setenv("AGENDA_ENABLED", "true")
    monkeypatch.setenv("AGENDA_SLACK_TARGET_CHANNEL_ID", "D0")
    monkeypatch.setenv(
        "CALENDAR_APPS_SCRIPT_URL",
        "https://script.google.com/macros/s/AKfy/exec",
    )
    monkeypatch.setenv("CALENDAR_APPS_SCRIPT_SHARED_TOKEN", "shared-secret")

    runner = AgendaRunner(
        settings=Settings(),
        slack_client=MagicMock(),
        llm_backend=MagicMock(),
        calendar_factory=None,
        docs_factory=lambda: None,
    )
    runner.start()
    assert runner._thread is not None
    runner.stop()


def test_normalise_event_synthesises_stable_id_for_apps_script_payload():
    """FR-CR-05-165: Apps Script proxy returns events without `id`
    (title + time only). Runner synthesises
    `agenda_synth:<title>:<start>` so idempotency works across
    ticks of the SAME event."""
    from app.agenda.runner import AgendaRunner

    start = datetime(2026, 5, 13, 15, 0, tzinfo=timezone.utc)
    apps_script_event = {
        "title": "Weekly sync",
        "start": start,
        "attendees": ["Артем"],
    }
    n1 = AgendaRunner._normalise_event(apps_script_event)
    n2 = AgendaRunner._normalise_event(apps_script_event)
    assert n1 is not None and n2 is not None
    assert n1["id"] == n2["id"]
    assert n1["id"].startswith("agenda_synth:weekly sync:")
    assert n1["title"] == "Weekly sync"


def test_normalise_event_keeps_native_id_when_present():
    from app.agenda.runner import AgendaRunner

    api_event = {
        "id": "abc123",
        "title": "Weekly sync",
        "start": datetime(2026, 5, 13, 15, 0, tzinfo=timezone.utc),
    }
    n = AgendaRunner._normalise_event(api_event)
    assert n is not None
    assert n["id"] == "abc123"


def test_normalise_event_returns_none_on_missing_fields():
    from app.agenda.runner import AgendaRunner

    assert AgendaRunner._normalise_event(
        {"start": datetime(2026, 5, 13, tzinfo=timezone.utc)}
    ) is None
    assert AgendaRunner._normalise_event({"title": "x"}) is None


# -- SA Calendar fallback (FR-CR-05-165 follow-up) ---------------------------


def test_composite_calendar_factory_prefers_oauth_when_available(monkeypatch):
    """When OAuth load returns valid creds, the composite factory
    must NOT touch the SA path."""
    from app.sync.factories import (
        build_calendar_credentials_factory_with_sa_fallback,
    )

    monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_SECRET", "y")

    oauth_called = {"n": 0}
    sa_called = {"n": 0}

    def fake_oauth_factory(s):
        def inner():
            oauth_called["n"] += 1
            return "OAUTH_CREDS_OBJ"
        return inner

    def fake_sa_factory(s):
        def inner():
            sa_called["n"] += 1
            return "SA_CREDS_OBJ"
        return inner

    monkeypatch.setattr(
        "app.sync.factories.build_calendar_credentials_factory",
        fake_oauth_factory,
    )
    monkeypatch.setattr(
        "app.sync.factories.build_calendar_sa_credentials_factory",
        fake_sa_factory,
    )

    from app.config import Settings

    factory = build_calendar_credentials_factory_with_sa_fallback(Settings())
    assert factory is not None
    creds = factory()
    assert creds == "OAUTH_CREDS_OBJ"
    assert oauth_called["n"] == 1
    assert sa_called["n"] == 0


def test_composite_calendar_factory_falls_back_to_sa_on_oauth_failure(monkeypatch):
    """FR-CR-05-165 — when the OAuth inner factory raises
    (e.g. `unauthorized_client` after Client Secret rotation),
    composite must catch + try SA path."""
    from app.sync.factories import (
        build_calendar_credentials_factory_with_sa_fallback,
    )

    def fake_oauth_factory(s):
        def inner():
            raise RuntimeError("unauthorized_client: rotated")
        return inner

    def fake_sa_factory(s):
        def inner():
            return "SA_CREDS_OBJ"
        return inner

    monkeypatch.setattr(
        "app.sync.factories.build_calendar_credentials_factory",
        fake_oauth_factory,
    )
    monkeypatch.setattr(
        "app.sync.factories.build_calendar_sa_credentials_factory",
        fake_sa_factory,
    )

    from app.config import Settings

    factory = build_calendar_credentials_factory_with_sa_fallback(Settings())
    assert factory is not None
    creds = factory()
    assert creds == "SA_CREDS_OBJ"


def test_composite_calendar_factory_falls_back_when_oauth_refresh_fails(monkeypatch):
    """FR-CR-05-165 — most production failures show up only when
    googleapiclient tries to refresh (e.g. `unauthorized_client`
    after a Client Secret rotation). The composite factory eagerly
    triggers refresh and falls back to SA on failure."""
    from app.sync.factories import (
        build_calendar_credentials_factory_with_sa_fallback,
    )

    class FakeOAuthCreds:
        valid = False  # force the refresh path

        def refresh(self, _request):
            raise RuntimeError("unauthorized_client: rotated")

    def fake_oauth_factory(s):
        def inner():
            return FakeOAuthCreds()
        return inner

    def fake_sa_factory(s):
        def inner():
            return "SA_CREDS_OBJ"
        return inner

    monkeypatch.setattr(
        "app.sync.factories.build_calendar_credentials_factory",
        fake_oauth_factory,
    )
    monkeypatch.setattr(
        "app.sync.factories.build_calendar_sa_credentials_factory",
        fake_sa_factory,
    )

    from app.config import Settings

    factory = build_calendar_credentials_factory_with_sa_fallback(Settings())
    assert factory is not None
    creds = factory()
    assert creds == "SA_CREDS_OBJ"


def test_composite_calendar_factory_returns_none_when_both_unavailable(monkeypatch):
    """Both inner factories return None → composite itself is
    None (so the runner's no-source guard fires correctly)."""
    from app.sync.factories import (
        build_calendar_credentials_factory_with_sa_fallback,
    )

    monkeypatch.setattr(
        "app.sync.factories.build_calendar_credentials_factory",
        lambda s: None,
    )
    monkeypatch.setattr(
        "app.sync.factories.build_calendar_sa_credentials_factory",
        lambda s: None,
    )

    from app.config import Settings

    factory = build_calendar_credentials_factory_with_sa_fallback(Settings())
    assert factory is None


# -- FR-CR-05-166 — calendar-less zoom-pattern source ----------------------


def _zr(
    zoom_id: str,
    title: str,
    meeting_date: datetime,
    host_email: str | None = None,
) -> ZoomRecording:
    return ZoomRecording(
        zoom_id=zoom_id,
        title=title,
        meeting_date=meeting_date,
        host_email=host_email,
    )


def test_zoom_pattern_predicts_next_weekly_instance(session):
    """3 weekly instances on Tuesdays at 15:00 UTC → predict next
    Tuesday at 15:00 UTC. Target window centered on that
    prediction → predict_upcoming_events returns one event."""
    from app.agenda.zoom_pattern import predict_upcoming_events

    # Tuesdays: 2026-04-28, 2026-05-05, 2026-05-12
    base = [
        datetime(2026, 4, 28, 15, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 5, 15, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 12, 15, 0, tzinfo=timezone.utc),
    ]
    session.add_all(
        [_zr(f"z{i}", "Weekly sync", b) for i, b in enumerate(base)]
    )
    session.flush()

    # Tick at 14:50 on the next Tuesday (2026-05-19). lead_time=10
    # so target_dt = 15:00, window=1.
    target = datetime(2026, 5, 19, 15, 0, tzinfo=timezone.utc)
    events = predict_upcoming_events(
        session, target_dt=target, window_minutes=1, lookback_days=60,
        min_prior_meetings=2,
    )
    assert len(events) == 1
    ev = events[0]
    assert ev["title"] == "Weekly sync"
    assert ev["start"] == target
    assert ev["id"].startswith("agenda_synth:weekly sync:")


def test_zoom_pattern_detects_daily_groups(session):
    """FR-CR-05-166 follow-up: daily-cadence series (operator's
    «Подземелья» style) are detected too. Two days in a row at
    the same time → predict next day."""
    from app.agenda.zoom_pattern import predict_upcoming_events

    session.add_all(
        [
            _zr(
                "a",
                "Daily standup",
                datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc),
            ),
            _zr(
                "b",
                "Daily standup",
                datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc),
            ),
        ]
    )
    session.flush()

    # Next predicted instance = 2026-05-13 12:00 (last+1 day).
    target = datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc)
    events = predict_upcoming_events(
        session, target_dt=target, window_minutes=1, lookback_days=30,
        min_prior_meetings=2,
    )
    assert len(events) == 1
    assert events[0]["title"] == "Daily standup"
    assert events[0]["start"] == target


def test_zoom_pattern_skips_irregular_groups(session):
    """Two recordings 3 days apart fit NO known cadence (1, 7,
    or 14 days) → no prediction."""
    from app.agenda.zoom_pattern import predict_upcoming_events

    session.add_all(
        [
            _zr(
                "a",
                "Irregular chat",
                datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc),
            ),
            _zr(
                "b",
                "Irregular chat",
                datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc),
            ),
        ]
    )
    session.flush()

    target = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
    assert (
        predict_upcoming_events(
            session, target_dt=target, window_minutes=1, lookback_days=30,
            min_prior_meetings=2,
        )
        == []
    )


def test_zoom_pattern_filters_by_host_email(session):
    """FR-CR-05-166 — when `host_email` filter is passed, only
    recordings hosted by that email participate in pattern
    detection. Operator-pinned: «надо делать такие агенды где
    хост 1@thehumanoid.ai»."""
    from app.agenda.zoom_pattern import predict_upcoming_events

    # Daily Иринины «Подземелья» — should NOT yield an agenda.
    session.add_all(
        [
            _zr(
                "irina1",
                "Подземелья",
                datetime(2026, 5, 11, 9, 0, tzinfo=timezone.utc),
                host_email="irina@thehumanoid.ai",
            ),
            _zr(
                "irina2",
                "Подземелья",
                datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc),
                host_email="irina@thehumanoid.ai",
            ),
        ]
    )
    # Weekly Артемова встреча — SHOULD yield.
    session.add_all(
        [
            _zr(
                "artem1",
                "Genia Xasis weekly",
                datetime(2026, 5, 5, 15, 0, tzinfo=timezone.utc),
                host_email="1@thehumanoid.ai",
            ),
            _zr(
                "artem2",
                "Genia Xasis weekly",
                datetime(2026, 5, 12, 15, 0, tzinfo=timezone.utc),
                host_email="1@thehumanoid.ai",
            ),
        ]
    )
    session.flush()

    # Target: next Tuesday 15:00 — both daily (predict 13/05 09:00)
    # and weekly (predict 19/05 15:00) would NORMALLY produce
    # events. With host filter, only Артемова weekly survives,
    # and only because target_dt is set to its predicted next
    # instance.
    target = datetime(2026, 5, 19, 15, 0, tzinfo=timezone.utc)
    events = predict_upcoming_events(
        session, target_dt=target, window_minutes=1, lookback_days=30,
        min_prior_meetings=2,
        host_email="1@thehumanoid.ai",
    )
    assert len(events) == 1
    assert events[0]["title"] == "Genia Xasis weekly"


def test_zoom_pattern_skips_null_host_when_filter_set(session):
    """FR-CR-05-166 — recordings with NULL `host_email` (old rows
    pre-migration) are skipped when the filter is active. Better
    safe than sending an agenda for somebody else's meeting."""
    from app.agenda.zoom_pattern import predict_upcoming_events

    session.add_all(
        [
            _zr(
                "x1",
                "Old recording",
                datetime(2026, 5, 5, 15, 0, tzinfo=timezone.utc),
                host_email=None,
            ),
            _zr(
                "x2",
                "Old recording",
                datetime(2026, 5, 12, 15, 0, tzinfo=timezone.utc),
                host_email=None,
            ),
        ]
    )
    session.flush()

    target = datetime(2026, 5, 19, 15, 0, tzinfo=timezone.utc)
    assert (
        predict_upcoming_events(
            session, target_dt=target, window_minutes=1, lookback_days=30,
            min_prior_meetings=2,
            host_email="1@thehumanoid.ai",
        )
        == []
    )


def test_zoom_pattern_no_filter_picks_all_hosts(session):
    """Without host_email filter (e.g. agenda for everybody) the
    detector continues to operate on the full set."""
    from app.agenda.zoom_pattern import predict_upcoming_events

    session.add_all(
        [
            _zr(
                "x1",
                "Mixed",
                datetime(2026, 5, 5, 15, 0, tzinfo=timezone.utc),
                host_email="alice@thehumanoid.ai",
            ),
            _zr(
                "x2",
                "Mixed",
                datetime(2026, 5, 12, 15, 0, tzinfo=timezone.utc),
                host_email="bob@thehumanoid.ai",
            ),
        ]
    )
    session.flush()

    target = datetime(2026, 5, 19, 15, 0, tzinfo=timezone.utc)
    events = predict_upcoming_events(
        session, target_dt=target, window_minutes=1, lookback_days=30,
        min_prior_meetings=2,
    )
    assert len(events) == 1
    assert events[0]["title"] == "Mixed"


def test_zoom_pattern_detects_biweekly_groups(session):
    """FR-CR-05-166 follow-up: bi-weekly cadence (every 14 days
    same weekday + time) is detected too."""
    from app.agenda.zoom_pattern import predict_upcoming_events

    # Tuesdays 2026-04-21, 2026-05-05, 2026-05-19 — 14 days apart
    session.add_all(
        [
            _zr(
                "a",
                "Biweekly review",
                datetime(2026, 4, 21, 15, 0, tzinfo=timezone.utc),
            ),
            _zr(
                "b",
                "Biweekly review",
                datetime(2026, 5, 5, 15, 0, tzinfo=timezone.utc),
            ),
            _zr(
                "c",
                "Biweekly review",
                datetime(2026, 5, 19, 15, 0, tzinfo=timezone.utc),
            ),
        ]
    )
    session.flush()

    target = datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc)
    events = predict_upcoming_events(
        session, target_dt=target, window_minutes=1, lookback_days=90,
        min_prior_meetings=2,
    )
    assert len(events) == 1
    assert events[0]["title"] == "Biweekly review"


def test_zoom_pattern_requires_min_prior_meetings(session):
    """A single recording is never a pattern even when alone in a
    title-group."""
    from app.agenda.zoom_pattern import predict_upcoming_events

    session.add(
        _zr(
            "solo",
            "One-off sync",
            datetime(2026, 5, 12, 15, 0, tzinfo=timezone.utc),
        ),
    )
    session.flush()

    target = datetime(2026, 5, 19, 15, 0, tzinfo=timezone.utc)
    assert (
        predict_upcoming_events(
            session, target_dt=target, window_minutes=1, lookback_days=30,
            min_prior_meetings=2,
        )
        == []
    )


def test_zoom_pattern_synth_id_is_stable_across_calls(session):
    """Idempotency invariant: calling predict_upcoming_events twice
    for the same target time MUST produce the same `id` so the
    `meeting_agendas` UNIQUE on `calendar_event_id` keeps the
    second tick from sending a duplicate."""
    from app.agenda.zoom_pattern import predict_upcoming_events

    base = [
        datetime(2026, 4, 28, 15, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 5, 15, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 12, 15, 0, tzinfo=timezone.utc),
    ]
    session.add_all(
        [_zr(f"z{i}", "Weekly sync", b) for i, b in enumerate(base)]
    )
    session.flush()

    target = datetime(2026, 5, 19, 15, 0, tzinfo=timezone.utc)
    e1 = predict_upcoming_events(
        session, target_dt=target, window_minutes=1, lookback_days=60,
        min_prior_meetings=2,
    )
    e2 = predict_upcoming_events(
        session, target_dt=target, window_minutes=1, lookback_days=60,
        min_prior_meetings=2,
    )
    assert len(e1) == 1 and len(e2) == 1
    assert e1[0]["id"] == e2[0]["id"]


def test_zoom_pattern_window_misses_when_target_off(session):
    """When `target_dt` is off the predicted instance by more than
    `window_minutes`, the prediction is not returned."""
    from app.agenda.zoom_pattern import predict_upcoming_events

    base = [
        datetime(2026, 4, 28, 15, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 5, 15, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 12, 15, 0, tzinfo=timezone.utc),
    ]
    session.add_all(
        [_zr(f"z{i}", "Weekly sync", b) for i, b in enumerate(base)]
    )
    session.flush()

    # Predicted next is 2026-05-19 15:00 UTC. Tick at 09:00 same
    # day with window=1min → miss.
    target = datetime(2026, 5, 19, 9, 0, tzinfo=timezone.utc)
    assert (
        predict_upcoming_events(
            session, target_dt=target, window_minutes=1, lookback_days=60,
            min_prior_meetings=2,
        )
        == []
    )


def test_runner_starts_with_zoom_pattern_source_no_calendar(monkeypatch):
    """FR-CR-05-166 — when AGENDA_SOURCE=zoom_pattern, the runner
    starts even without ANY calendar source (no OAuth, no SA, no
    Apps Script)."""
    from app.agenda.runner import AgendaRunner
    from app.config import Settings

    monkeypatch.setenv("AGENDA_ENABLED", "true")
    monkeypatch.setenv("AGENDA_SLACK_TARGET_CHANNEL_ID", "D0")
    monkeypatch.setenv("AGENDA_SOURCE", "zoom_pattern")

    runner = AgendaRunner(
        settings=Settings(),
        slack_client=MagicMock(),
        llm_backend=MagicMock(),
        calendar_factory=None,
        docs_factory=lambda: None,
    )
    runner.start()
    assert runner._thread is not None
    runner.stop()
