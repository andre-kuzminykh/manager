"""FR-CR-05-193d-1 — ID-locked tests для TeamMember.notes DSL parser."""
from __future__ import annotations


def test_fr_cr_05_193d_parse_delegate_marker() -> None:
    """Marker `DELEGATE_TASKS_TO: <real_name>` извлекается из notes."""
    from app.services.team_member_notes_dsl import parse_notes_dsl

    parsed = parse_notes_dsl(
        "Только стратегические вопросы. DELEGATE_TASKS_TO: Irina Shipilova. Прочее..."
    )
    assert parsed["delegate_to"] == "Irina Shipilova"


def test_fr_cr_05_193d_parse_do_not_call() -> None:
    """Marker `DO_NOT_CALL` (case-insensitive) → do_not_call=True."""
    from app.services.team_member_notes_dsl import parse_notes_dsl

    for notes in (
        "Юрист. DO_NOT_CALL.",
        "Не вызывай никогда. do_not_call",
        "DO_NOT_CALL — никогда",
    ):
        assert parse_notes_dsl(notes)["do_not_call"] is True


def test_fr_cr_05_193d_no_markers_returns_defaults() -> None:
    """Notes без DSL маркеров → delegate_to=None, do_not_call=False."""
    from app.services.team_member_notes_dsl import parse_notes_dsl

    parsed = parse_notes_dsl("Просто рабочие заметки без маркеров")
    assert parsed["delegate_to"] is None
    assert parsed["do_not_call"] is False


def test_fr_cr_05_193d_empty_or_none_notes() -> None:
    """None / empty / whitespace → defaults без exception."""
    from app.services.team_member_notes_dsl import parse_notes_dsl

    for empty in (None, "", "   ", "\n\t"):
        parsed = parse_notes_dsl(empty)
        assert parsed == {"delegate_to": None, "do_not_call": False}


def test_fr_cr_05_193d_delegate_marker_case_insensitive() -> None:
    """`DELEGATE_TASKS_TO:` vs `delegate_tasks_to:` vs mixed → одинаково parsed."""
    from app.services.team_member_notes_dsl import parse_notes_dsl

    for notes in (
        "DELEGATE_TASKS_TO: Ирина",
        "delegate_tasks_to: Ирина",
        "Delegate_Tasks_To:    Ирина  ",
    ):
        assert parse_notes_dsl(notes)["delegate_to"] == "Ирина"
