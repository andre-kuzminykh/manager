"""FR-CR-05-136 — Calendar-match for meeting titles.

Covers the three layers:

1. `fetch_calendar_events_around` — Apps Script HTTP proxy
   round-trip, with shared-token + ISO date range. Failures
   never raise.

2. `match_calendar_event_to_meeting_via_llm` — LLM picks the
   best candidate or `null`. Hallucinated indices dropped.

3. `format_canonical_title` — `DD/MM - <title>` shape.

Plus end-to-end: the Fireflies pipeline rewrites
`MeetingRecording.title` after `_step_match_calendar_title`.
"""
from __future__ import annotations

import contextlib
import json
import sys
import types
from datetime import datetime, timezone
from io import BytesIO
from unittest.mock import patch

import pytest

from app.services.calendar_match import (
    fetch_calendar_events_around,
    format_canonical_title,
    match_and_format_title,
    match_calendar_event_to_meeting_via_llm,
)


@contextlib.contextmanager
def _patched_googleapiclient(service_factory):
    """FR-CR-05-144 — install a stub `googleapiclient` package
    in `sys.modules` so the lazy `from googleapiclient.discovery
    import build` inside `fetch_calendar_events_via_api` resolves
    to our stub. Restores prior modules on exit so order-
    dependent test pollution doesn't leak."""
    saved = {
        k: sys.modules.get(k)
        for k in (
            "googleapiclient",
            "googleapiclient.discovery",
            "googleapiclient.errors",
        )
    }
    stub_disc = types.SimpleNamespace(
        build=lambda *a, **kw: service_factory(),
    )
    stub_errors = types.SimpleNamespace(HttpError=Exception)
    sys.modules["googleapiclient"] = types.SimpleNamespace(
        discovery=stub_disc, errors=stub_errors,
    )
    sys.modules["googleapiclient.discovery"] = stub_disc
    sys.modules["googleapiclient.errors"] = stub_errors
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


# --- _format_canonical_title ----------------------------------

def test_format_canonical_title_dd_mm_dash_title():
    """Operator-pinned shape: «30/04 - US Innovative Technology»."""
    dt = datetime(2026, 4, 30, 14, 30, tzinfo=timezone.utc)
    assert format_canonical_title(dt, "US Innovative Technology") \
        == "30/04 - US Innovative Technology"


def test_format_canonical_title_pads_single_digit_day_and_month():
    dt = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)
    assert format_canonical_title(dt, "Sync") == "05/01 - Sync"


def test_format_canonical_title_strips_picked_whitespace():
    dt = datetime(2026, 4, 30, tzinfo=timezone.utc)
    assert format_canonical_title(dt, "  TWG  ") == "30/04 - TWG"


def test_format_canonical_title_empty_picked_returns_empty():
    dt = datetime(2026, 4, 30, tzinfo=timezone.utc)
    assert format_canonical_title(dt, "") == ""


# --- fetch_calendar_events_around -----------------------------

class _FakeURLOpenResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def test_fetch_calendar_events_around_round_trips_apps_script():
    """The Apps Script Web App returns
    `{events: [...]}`; we forward the inner list verbatim."""
    captured_url: list[str] = []

    fake_payload = {
        "events": [
            {
                "title": "TWG Global <> Humanoid",
                "start": "2026-04-30T14:30:00Z",
                "end": "2026-04-30T15:30:00Z",
                "attendees": [
                    {"email": "alina@humanoid.ai", "name": "Alina"}
                ],
                "description": "Intro call",
            }
        ]
    }

    def fake_urlopen(url, *, timeout):
        captured_url.append(url)
        return _FakeURLOpenResponse(json.dumps(fake_payload).encode())

    with patch("app.services.calendar_match.urllib.request.urlopen",
               side_effect=fake_urlopen):
        out = fetch_calendar_events_around(
            datetime(2026, 4, 30, 14, 30, tzinfo=timezone.utc),
            window_minutes=30,
            apps_script_url="https://script.google.com/macros/s/AAA/exec",
            shared_token="topsecret",
        )

    assert len(captured_url) == 1
    url = captured_url[0]
    # Shared-token threaded through query string (operator-pinned auth).
    assert "token=topsecret" in url
    # ±30 min window encoded.
    assert "from=2026-04-30T14%3A00%3A00" in url
    assert "to=2026-04-30T15%3A00%3A00" in url
    # Result forwarded.
    assert len(out) == 1
    assert out[0]["title"] == "TWG Global <> Humanoid"


def test_fetch_calendar_events_disabled_when_url_missing():
    out = fetch_calendar_events_around(
        datetime(2026, 4, 30, tzinfo=timezone.utc),
        window_minutes=30, apps_script_url="", shared_token="x",
    )
    assert out == []


def test_fetch_calendar_events_returns_empty_on_http_error():
    """Apps Script 5xx, network timeout, JSON parse error — all
    must NOT cascade. Caller treats as «no candidates»."""
    import urllib.error

    def fake_urlopen(url, *, timeout):
        raise urllib.error.HTTPError(
            url, 500, "Server error", hdrs=None, fp=None
        )

    with patch("app.services.calendar_match.urllib.request.urlopen",
               side_effect=fake_urlopen):
        out = fetch_calendar_events_around(
            datetime(2026, 4, 30, tzinfo=timezone.utc),
            window_minutes=30,
            apps_script_url="https://script.google.com/exec",
            shared_token="x",
        )
    assert out == []


def test_fetch_calendar_events_passes_through_apps_script_error():
    """When Apps Script returns `{error: 'unauthorized'}` we
    treat it as no candidates and log."""
    def fake_urlopen(url, *, timeout):
        return _FakeURLOpenResponse(json.dumps(
            {"error": "unauthorized"}).encode())

    with patch("app.services.calendar_match.urllib.request.urlopen",
               side_effect=fake_urlopen):
        out = fetch_calendar_events_around(
            datetime(2026, 4, 30, tzinfo=timezone.utc),
            window_minutes=30,
            apps_script_url="https://script.google.com/exec",
            shared_token="x",
        )
    assert out == []


# --- match_calendar_event_to_meeting_via_llm -----------------

class _StubLLM:
    def __init__(self, picked_index, reason="ok"):
        self._picked = picked_index
        self._reason = reason
        self.last_user_prompt = None

    def complete_text(self, *, system_prompt, user_prompt, **_):
        self.last_user_prompt = user_prompt
        return json.dumps(
            {"picked_index": self._picked, "reason": self._reason}
        )


def test_match_via_llm_picks_indexed_event():
    events = [
        {"title": "Sync Alfa", "start": "2026-04-30T14:00Z",
         "attendees": [], "description": ""},
        {"title": "TWG Global <> Humanoid", "start": "2026-04-30T14:30Z",
         "attendees": [{"email": "alina@humanoid.ai", "name": "Alina"}],
         "description": "Intro"},
    ]
    llm = _StubLLM(picked_index=2)
    picked = match_calendar_event_to_meeting_via_llm(
        agenda="Discussed TWG Global investment thesis…",
        events=events, llm_backend=llm, model="gpt-5.5",
    )
    assert picked is not None
    assert picked["title"] == "TWG Global <> Humanoid"
    # Agenda + candidates rendered into the user prompt.
    assert "TWG Global" in (llm.last_user_prompt or "")
    assert "alina@humanoid.ai" in (llm.last_user_prompt or "")


def test_match_via_llm_returns_none_when_picked_index_null():
    events = [{"title": "Internal sync", "start": "...",
               "attendees": [], "description": ""}]
    llm = _StubLLM(picked_index=None)
    assert match_calendar_event_to_meeting_via_llm(
        agenda="Generic discussion", events=events,
        llm_backend=llm, model="gpt-5.5",
    ) is None


def test_match_via_llm_drops_invalid_index_above_range():
    events = [{"title": "A", "start": "x", "attendees": [], "description": ""}]
    llm = _StubLLM(picked_index=5)
    assert match_calendar_event_to_meeting_via_llm(
        agenda="x", events=events,
        llm_backend=llm, model="gpt-5.5",
    ) is None


def test_match_via_llm_handles_invalid_json_gracefully():
    class _BadLLM:
        def complete_text(self, **_):
            return "not json {"

    assert match_calendar_event_to_meeting_via_llm(
        agenda="x",
        events=[{"title": "A", "start": "x", "attendees": [], "description": ""}],
        llm_backend=_BadLLM(), model="gpt-5.5",
    ) is None


def test_match_via_llm_returns_none_on_empty_events_or_agenda():
    llm = _StubLLM(picked_index=1)
    assert match_calendar_event_to_meeting_via_llm(
        agenda="", events=[{"title": "A"}],
        llm_backend=llm, model="gpt-5.5",
    ) is None
    assert match_calendar_event_to_meeting_via_llm(
        agenda="x", events=[],
        llm_backend=llm, model="gpt-5.5",
    ) is None


# --- match_and_format_title (end-to-end) ----------------------

def test_match_and_format_title_returns_canonical_form_on_match():
    fake_payload = {
        "events": [
            {"title": "US Innovative Technology / TWG Global",
             "start": "2026-04-30T14:30Z",
             "attendees": [], "description": ""},
        ]
    }

    def fake_urlopen(url, *, timeout):
        return _FakeURLOpenResponse(json.dumps(fake_payload).encode())

    with patch("app.services.calendar_match.urllib.request.urlopen",
               side_effect=fake_urlopen):
        out = match_and_format_title(
            meeting_dt=datetime(2026, 4, 30, 14, 30, tzinfo=timezone.utc),
            agenda="TWG Global investment talk…",
            apps_script_url="https://script.google.com/exec",
            shared_token="x",
            window_minutes=30,
            llm_backend=_StubLLM(picked_index=1),
            model="gpt-5.5",
        )
    assert out == "30/04 - US Innovative Technology / TWG Global"


def test_match_and_format_title_returns_none_when_no_events():
    def fake_urlopen(url, *, timeout):
        return _FakeURLOpenResponse(json.dumps({"events": []}).encode())

    with patch("app.services.calendar_match.urllib.request.urlopen",
               side_effect=fake_urlopen):
        out = match_and_format_title(
            meeting_dt=datetime(2026, 4, 30, tzinfo=timezone.utc),
            agenda="x",
            apps_script_url="https://script.google.com/exec",
            shared_token="x",
            window_minutes=30,
            llm_backend=_StubLLM(picked_index=1),
            model="gpt-5.5",
        )
    assert out is None


def test_match_and_format_title_returns_none_when_llm_declines():
    fake_payload = {"events": [
        {"title": "Internal sync",
         "start": "2026-04-30T14:30Z",
         "attendees": [], "description": ""}
    ]}

    def fake_urlopen(url, *, timeout):
        return _FakeURLOpenResponse(json.dumps(fake_payload).encode())

    with patch("app.services.calendar_match.urllib.request.urlopen",
               side_effect=fake_urlopen):
        out = match_and_format_title(
            meeting_dt=datetime(2026, 4, 30, tzinfo=timezone.utc),
            agenda="Generic team chat",
            apps_script_url="https://script.google.com/exec",
            shared_token="x",
            window_minutes=30,
            llm_backend=_StubLLM(picked_index=None),
            model="gpt-5.5",
        )
    assert out is None


# --- FR-CR-05-144: direct Google Calendar API path ------------


def test_fetch_calendar_events_via_api_returns_normalised_dicts():
    """FR-CR-05-144 — direct Calendar API path returns events
    in the same shape as the Apps Script proxy
    (`{title, start, end, attendees, description}`) so the
    LLM-pass + format code is unchanged."""
    from app.services.calendar_match import fetch_calendar_events_via_api

    captured: dict = {}

    class _StubExecutable:
        def __init__(self, payload):
            self._payload = payload

        def execute(self):
            return self._payload

    class _StubEvents:
        def list(self, **kw):
            captured["list_kwargs"] = kw
            return _StubExecutable({
                "items": [
                    {
                        "summary": "Fundraising sync",
                        "start": {"dateTime": "2026-05-01T08:04:00Z"},
                        "end": {"dateTime": "2026-05-01T09:14:00Z"},
                        "attendees": [{"email": "a@b.c"}],
                        "description": "agenda…",
                    },
                    # All-day event uses `date` not `dateTime`.
                    {
                        "summary": "All-day",
                        "start": {"date": "2026-05-01"},
                        "end": {"date": "2026-05-02"},
                    },
                ]
            })

    class _StubService:
        def events(self):
            return _StubEvents()

    creds_returned: list = []

    def factory():
        creds_returned.append("called")
        return object()  # any non-None object

    with _patched_googleapiclient(_StubService):
        out = fetch_calendar_events_via_api(
            datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc),
            window_minutes=30,
            credentials_factory=factory,
            calendar_id="primary",
        )

    assert len(out) == 2
    assert out[0]["title"] == "Fundraising sync"
    assert out[0]["start"] == "2026-05-01T08:04:00Z"
    assert out[0]["end"] == "2026-05-01T09:14:00Z"
    assert out[0]["attendees"] == [{"email": "a@b.c"}]
    assert out[0]["description"] == "agenda…"
    # All-day event picks `date` field.
    assert out[1]["start"] == "2026-05-01"
    assert out[1]["end"] == "2026-05-02"
    # Calendar API was called with the right time window.
    list_kw = captured["list_kwargs"]
    assert list_kw["calendarId"] == "primary"
    assert list_kw["singleEvents"] is True
    assert list_kw["orderBy"] == "startTime"
    assert list_kw["timeMin"] == "2026-05-01T07:30:00+00:00"
    assert list_kw["timeMax"] == "2026-05-01T08:30:00+00:00"
    assert creds_returned == ["called"]


def test_fetch_calendar_events_via_api_no_credentials_returns_empty():
    """When the factory returns None (no stored OAuth record),
    the call returns empty without ever hitting the API."""
    from app.services.calendar_match import fetch_calendar_events_via_api

    out = fetch_calendar_events_via_api(
        datetime(2026, 5, 1, tzinfo=timezone.utc),
        window_minutes=30,
        credentials_factory=lambda: None,
    )
    assert out == []


def test_fetch_calendar_events_via_api_credentials_factory_raises():
    """Factory raising → empty list, never propagates."""
    from app.services.calendar_match import fetch_calendar_events_via_api

    def boom():
        raise RuntimeError("oauth token corrupt")

    out = fetch_calendar_events_via_api(
        datetime(2026, 5, 1, tzinfo=timezone.utc),
        window_minutes=30,
        credentials_factory=boom,
    )
    assert out == []


def test_match_and_format_title_prefers_api_path_when_factory_given():
    """FR-CR-05-144 — when both `api_credentials_factory` and
    `apps_script_url` are passed, the API path WINS — Apps
    Script proxy is the legacy fallback."""
    from app.services.calendar_match import match_and_format_title

    api_calls: list = []

    def factory():
        api_calls.append("called")
        return object()

    class _StubExecutable:
        def execute(self):
            return {"items": [
                {"summary": "01/05 sync",
                 "start": {"dateTime": "2026-05-01T08:00:00Z"}},
            ]}

    class _StubEvents:
        def list(self, **kw):
            return _StubExecutable()

    class _StubService:
        def events(self):
            return _StubEvents()

    # Apps Script `urlopen` MUST NOT be called — set up a
    # poison-trap that fails the test if it runs.
    def _poison(*a, **kw):
        raise AssertionError(
            "Apps Script urlopen called when API factory was given"
        )

    with _patched_googleapiclient(_StubService), patch(
        "app.services.calendar_match.urllib.request.urlopen",
        side_effect=_poison,
    ):
        out = match_and_format_title(
            meeting_dt=datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc),
            agenda="01/05 fundraising sync agenda",
            window_minutes=30,
            llm_backend=_StubLLM(picked_index=1),
            model="gpt-5.5",
            api_credentials_factory=factory,
            api_calendar_id="primary",
            # Apps Script also configured but should be ignored.
            apps_script_url="https://script.google.com/exec",
            shared_token="x",
        )
    assert out == "01/05 - 01/05 sync"
    assert api_calls == ["called"]


def test_match_and_format_title_no_op_when_neither_backend_configured():
    from app.services.calendar_match import match_and_format_title

    out = match_and_format_title(
        meeting_dt=datetime(2026, 5, 1, tzinfo=timezone.utc),
        agenda="x",
        window_minutes=30,
        llm_backend=_StubLLM(picked_index=1),
        model="gpt-5.5",
    )
    assert out is None
