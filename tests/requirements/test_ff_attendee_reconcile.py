"""FR-CR-05-253 — Fireflies reconciles calendar invitees against the actual
attendance signal (LLM-extracted speakers), dropping team no-shows while never
dropping externals or the operator. Pure unit tests of reconcile_team_attendees
(Kima regression: Jochen Rudat invited but absent).
"""
from __future__ import annotations

from app.services.calendar_attendees import reconcile_team_attendees


def _a(name, source="team_member", email=""):
    return {"resolved_name": name, "display_name": name,
            "source": source, "email": email}


# Kima: calendar had Artem + Jarad (present) + Jochen (invited, no-show).
_KIMA_CAL = [_a("Артем Соколов"), _a("Jarad Cannon"), _a("Jochen Rudat")]
_KIMA_SPEAKERS = ["Жойкина Наталья", "Jarad Cannon", "Артем Соколов"]


def test_drops_team_noshow() -> None:
    kept, dropped = reconcile_team_attendees(_KIMA_CAL, _KIMA_SPEAKERS)
    kept_names = [a["resolved_name"] for a in kept]
    dropped_names = [a["resolved_name"] for a in dropped]
    assert "Jochen Rudat" in dropped_names
    assert kept_names == ["Артем Соколов", "Jarad Cannon"]


def test_keeps_present_team_members() -> None:
    kept, dropped = reconcile_team_attendees(
        [_a("Артем Соколов")], ["Артем Соколов"])
    assert [a["resolved_name"] for a in kept] == ["Артем Соколов"]
    assert dropped == []


def test_never_drops_external() -> None:
    # an external (counterparty) invitee with no speaker match is KEPT
    cal = [_a("John Smith", source="counterparty"),
           _a("Mystery Guest", source="unknown")]
    kept, dropped = reconcile_team_attendees(cal, ["Артем Соколов"])
    assert dropped == []
    assert len(kept) == 2


def test_keeps_operator_by_email() -> None:
    cal = [_a("Operator Person", email="op@thehumanoid.ai")]
    # operator didn't surface as a speaker, but is kept via keep_emails
    kept, dropped = reconcile_team_attendees(
        cal, ["Someone Else"], keep_emails={"op@thehumanoid.ai"})
    assert dropped == []
    assert len(kept) == 1


def test_token_overlap_handles_partial_name() -> None:
    # speaker list carries only the surname → still a match
    kept, _ = reconcile_team_attendees([_a("Артем Соколов")], ["Соколов"])
    assert len(kept) == 1


def test_empty_speakers_drops_all_team_but_keeps_external() -> None:
    cal = [_a("Teammate One"), _a("External", source="counterparty")]
    kept, dropped = reconcile_team_attendees(cal, [])
    assert [a["resolved_name"] for a in kept] == ["External"]
    assert [a["resolved_name"] for a in dropped] == ["Teammate One"]


def test_non_dict_entries_passthrough() -> None:
    kept, dropped = reconcile_team_attendees(["raw-string", _a("Ghost")], [])
    assert "raw-string" in kept
    assert dropped and dropped[0]["resolved_name"] == "Ghost"


def test_none_inputs() -> None:
    assert reconcile_team_attendees(None, None) == ([], [])


__all__: list[str] = []
