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

import json
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
