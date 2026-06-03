"""FR-CR-05-191 v4 — people canonicalisation must NOT inject an employee
who did not attend the meeting.

Regression 2026-06-03 «Алина, Ирина»: an external fundraising contact
«Андре» was rewritten to the internal team member «Андрей Кузьминых»
(AI Lead, absent from the call). The fix restricts TeamMember candidates
to those whose surname appears in the meeting's participant roster.
"""
from __future__ import annotations

from app.services.summary_canonicalize import resolve_people_to_team_members


class _TM:
    def __init__(self, real_name: str) -> None:
        self.real_name = real_name
        self.active = True


class _Query:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):  # noqa: ANN002
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *a, **k):  # noqa: ANN002
        return _Query(self._rows)


_TEAM = [
    _TM("Андрей Кузьминых"),   # AI Lead — NOT in the fundraising call
    _TM("Артем Соколов"),      # actually present
]
_ROSTER = ["Alina Kolpakova", "Артем Соколов", "Irina Shipilova"]


def test_absent_employee_not_injected() -> None:
    s = _Session(_TEAM)
    # «Андрей» must NOT become «Андрей Кузьминых» — he wasn't there.
    out = resolve_people_to_team_members(
        ["Андрей"], s, participant_names=_ROSTER,
    )
    assert out == {}


def test_present_member_still_canonicalises() -> None:
    s = _Session(_TEAM)
    # «Артем» → «Артем Соколов» — he IS a participant (Соколов in roster).
    out = resolve_people_to_team_members(
        ["Артем"], s, participant_names=_ROSTER,
    )
    assert out.get("Артем") == "Артем Соколов"


def test_no_roster_keeps_legacy_behaviour() -> None:
    s = _Session(_TEAM)
    # Without a roster, the old behaviour stands (Андрей → Андрей Кузьминых).
    out = resolve_people_to_team_members(["Андрей"], s)
    assert out.get("Андрей") == "Андрей Кузьминых"


def test_roster_with_no_matching_member_resolves_nothing() -> None:
    s = _Session(_TEAM)
    out = resolve_people_to_team_members(
        ["Андрей"], s, participant_names=["Someone Else", "Other Person"],
    )
    assert out == {}


__all__: list[str] = []
