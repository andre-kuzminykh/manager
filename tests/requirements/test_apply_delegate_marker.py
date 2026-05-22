"""FR-CR-05-192r-live — ID-locked tests for the shared
`apply_delegate_marker` helper used by both the live
Fireflies/Zoom pipelines and the ephemeral send_one_* path.

Operator pinned 2026-05-22: «на артема вообще задачи не ставить,
на ирину — это надо в ноутс добавить чтобы знать». The marker
mechanism: a TeamMember whose `notes` carries
`DELEGATE_TASKS_TO: <real_name>` has all assigned tasks
silently re-attributed to the delegate (when delegate is itself
in the active TM directory).

Contract locked here:
  - case-insensitive marker match (`DELEGATE_TASKS_TO:` or
    `delegate_tasks_to:`)
  - capture stops at first `.` or `\\n` so trailing context in
    notes («. Operator-pinned…») doesn't bleed
  - single-hop only (no recursive lookup)
  - sanity guard: delegate target must itself be in
    `known_employees` AND have a slack_user_id
  - no-op when marker absent / target missing / target has no
    slack_user_id (caller's fallback chain kicks in)
"""
from __future__ import annotations

from app.services.team_members import apply_delegate_marker


def _ke(slack_user_id: str, real_name: str, notes: str = "") -> dict:
    return {
        "slack_user_id": slack_user_id,
        "real_name": real_name,
        "notes": notes,
        "role": "",
        "display_name": real_name,
    }


def test_fr_cr_05_192r_live_delegate_swaps_when_marker_present() -> None:
    ke = [
        _ke("U_ARTEM", "Артем Соколов",
            "DELEGATE_TASKS_TO: Ирина Шипилова. Operator-pinned."),
        _ke("U_IRINA", "Ирина Шипилова"),
    ]
    new_id, delegate = apply_delegate_marker("U_ARTEM", ke)
    assert new_id == "U_IRINA"
    assert delegate == "Ирина Шипилова"


def test_fr_cr_05_192r_live_delegate_marker_case_insensitive() -> None:
    ke = [
        _ke("U_ARTEM", "Артем Соколов",
            "delegate_tasks_to: Ирина Шипилова"),
        _ke("U_IRINA", "Ирина Шипилова"),
    ]
    new_id, delegate = apply_delegate_marker("U_ARTEM", ke)
    assert new_id == "U_IRINA"
    assert delegate == "Ирина Шипилова"


def test_fr_cr_05_192r_live_no_swap_when_marker_absent() -> None:
    ke = [
        _ke("U_DMITRY", "Дмитрий Седов", "CFO. No delegate."),
        _ke("U_IRINA", "Ирина Шипилова"),
    ]
    new_id, delegate = apply_delegate_marker("U_DMITRY", ke)
    assert new_id == "U_DMITRY"
    assert delegate is None


def test_fr_cr_05_192r_live_no_swap_when_delegate_target_missing() -> None:
    """Sanity guard: target must be in known_employees."""
    ke = [
        _ke("U_ARTEM", "Артем Соколов",
            "DELEGATE_TASKS_TO: Nonexistent Person"),
    ]
    new_id, delegate = apply_delegate_marker("U_ARTEM", ke)
    assert new_id == "U_ARTEM"
    assert delegate is None


def test_fr_cr_05_192r_live_no_swap_when_delegate_has_no_slack_uid() -> None:
    """If delegate is in TM but lacks slack_user_id (e.g. team#N
    placeholder yields empty), keep original. Caller's fallback
    chain handles the case."""
    ke = [
        _ke("U_ARTEM", "Артем Соколов",
            "DELEGATE_TASKS_TO: Ирина Шипилова"),
        {
            "slack_user_id": "",  # blank
            "real_name": "Ирина Шипилова",
            "notes": "",
            "role": "",
            "display_name": "Ирина Шипилова",
        },
    ]
    new_id, delegate = apply_delegate_marker("U_ARTEM", ke)
    assert new_id == "U_ARTEM"
    assert delegate is None


def test_fr_cr_05_192r_live_capture_stops_at_period() -> None:
    """Operator's notes typically end the delegate line with «.
    Operator-pinned…». The capture MUST stop at the first period
    so the trailing context doesn't become part of the delegate
    name (which would then fail TM-membership lookup)."""
    ke = [
        _ke(
            "U_ARTEM", "Артем Соколов",
            "DELEGATE_TASKS_TO: Ирина Шипилова. Operator-pinned 2026-05-22: "
            "CEO does not own actionable items.",
        ),
        _ke("U_IRINA", "Ирина Шипилова"),
    ]
    new_id, delegate = apply_delegate_marker("U_ARTEM", ke)
    assert new_id == "U_IRINA"
    assert delegate == "Ирина Шипилова"


def test_fr_cr_05_192r_live_empty_inputs_safe() -> None:
    assert apply_delegate_marker(None, []) == (None, None)
    assert apply_delegate_marker("", []) == ("", None)
    assert apply_delegate_marker("U_X", []) == ("U_X", None)


def test_fr_cr_05_192r_live_source_employee_not_in_known() -> None:
    """If owner_user_id isn't found in known_employees, return as-is."""
    ke = [_ke("U_IRINA", "Ирина Шипилова")]
    new_id, delegate = apply_delegate_marker("U_UNKNOWN", ke)
    assert new_id == "U_UNKNOWN"
    assert delegate is None


__all__ = []  # type: ignore[var-annotated]
