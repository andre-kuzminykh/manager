"""FR-CR-05-208 — Fireflies renames meetings, so calendar attendees must be
matched by TIME, not title.

The Fireflies bot gives the meeting its own semantic title («Mitsubishi:
роботы…») which never overlaps the Google Calendar event title («Kodai
Yamagishi … Zoom call»). Title-based fuzzy matching therefore fails and the
summary falls back to Fireflies' unreliable participant list (the internal
team). The fix: match the calendar event by time proximity (closest event
WITH attendees), so the authoritative invitee list is used.
"""
from __future__ import annotations

from datetime import datetime, timezone


def _ev(start_iso: str, title: str, attendees: list[dict]) -> dict:
    return {"summary": title, "start": start_iso, "attendees": attendees}


def test_fr_cr_05_208_time_only_matches_when_title_differs() -> None:
    from app.services.calendar_attendees import _find_matching_event

    md = datetime(2026, 5, 28, 8, 5, tzinfo=timezone.utc)
    events = [
        _ev(
            "2026-05-28T08:00:00+00:00",
            "Kodai Yamagishi (MC Global Innovation Inc) <> Humanoid | Zoom call",
            [{"email": "a@x.com", "displayName": "A"}],
        ),
    ]
    ev, method = _find_matching_event(
        events, zoom_meeting_id=None, meeting_date=md,
        meeting_title="Mitsubishi: роботы для заводов в Азии",
        allow_time_only=True,
    )
    assert method == "time" and ev is events[0]
    # Zoom behaviour preserved: WITHOUT allow_time_only a renamed meeting
    # does not match (title overlap still required).
    ev2, m2 = _find_matching_event(
        events, zoom_meeting_id=None, meeting_date=md,
        meeting_title="Mitsubishi: роботы для заводов в Азии",
        allow_time_only=False,
    )
    assert ev2 is None and m2 == ""


def test_fr_cr_05_208_time_only_skips_events_without_attendees() -> None:
    """«bedtime» / «Set your working location» carry no attendees → skipped;
    the real meeting (with attendees) is chosen."""
    from app.services.calendar_attendees import _find_matching_event

    md = datetime(2026, 5, 28, 8, 5, tzinfo=timezone.utc)
    events = [
        _ev("2026-05-28T08:04:00+00:00", "bedtime", []),
        _ev("2026-05-28T08:06:00+00:00", "Set your working location", []),
        _ev("2026-05-28T08:00:00+00:00", "Real meeting", [{"email": "a@x.com"}]),
    ]
    ev, method = _find_matching_event(
        events, zoom_meeting_id=None, meeting_date=md,
        meeting_title="Renamed by Fireflies", allow_time_only=True,
    )
    assert method == "time" and ev["summary"] == "Real meeting"


def test_fr_cr_05_208_time_only_picks_closest() -> None:
    from app.services.calendar_attendees import _find_matching_event

    md = datetime(2026, 5, 28, 8, 5, tzinfo=timezone.utc)
    events = [
        _ev("2026-05-28T08:14:00+00:00", "Far", [{"email": "f@x.com"}]),
        _ev("2026-05-28T08:03:00+00:00", "Near", [{"email": "n@x.com"}]),
    ]
    ev, _ = _find_matching_event(
        events, zoom_meeting_id=None, meeting_date=md,
        meeting_title="x", allow_time_only=True,
    )
    assert ev["summary"] == "Near"


def test_fr_cr_05_208_fireflies_resolver_resolves_calendar_names(monkeypatch) -> None:
    """End-to-end: a renamed Fireflies meeting resolves to the real calendar
    invitees, internal names canonicalized via the directory."""
    import app.services.calendar_attendees as M

    monkeypatch.setattr(
        M, "_build_email_to_name_map",
        lambda s: {"jochen.rudat@humanoid.ai": "Jochen Rudat"},
    )
    monkeypatch.setattr(M, "_build_counterparty_email_map", lambda s: {})

    md = datetime(2026, 5, 28, 8, 5, tzinfo=timezone.utc)
    events = [
        _ev(
            "2026-05-28T08:00:00+00:00",
            "Kodai Yamagishi <> Humanoid | Zoom call",
            [
                {"email": "jochen.rudat@humanoid.ai", "displayName": "Jochen Rudat"},
                {"email": "kodai.yamagishi@mitsubishicorp.com",
                 "displayName": "Kodai Yamagishi"},
            ],
        ),
    ]

    class Row:
        title = "Mitsubishi: роботы для заводов в Азии"
        meeting_date = md
        fireflies_id = "ff149"

    res = M.resolve_calendar_attendees_for_fireflies(
        Row(), object(), calendar_events=events,
    )
    assert res is not None
    assert res["match_method"] == "time"
    names = [a["resolved_name"] for a in res["attendees"]]
    assert "Jochen Rudat" in names          # internal → canonical directory name
    assert "Kodai Yamagishi" in names       # external → displayName
