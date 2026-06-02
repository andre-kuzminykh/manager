"""FR-CR-05-167 follow-up — ops.agenda_update_recap pure-function contract.

The tool patches a posted agenda's «На прошлой встрече: …» recap in place.
These cover the deterministic body-surgery + recap formatting (no Slack/DB).
"""
from __future__ import annotations

from ops.agenda_update_recap import _inject_recap, _render_recap_line

_POSTED_NO_RECAP = (
    "*<https://doc|02/06 - Агенда к Fundraising daily>*\n\n"
    "Участники: Артем Соколов, Alina Kolpakova\n\n"
    "Статус задач к обсуждению:"
)


def test_inject_recap_canonical_order():
    new = _inject_recap(_POSTED_NO_RECAP, "На прошлой встрече: Обсудили раунд.")
    assert "На прошлой встрече:" in new and "Статус задач к обсуждению:" in new
    # header → Участники → recap → Статус
    assert new.index("Участники:") < new.index("На прошлой встрече:")
    assert new.index("На прошлой встрече:") < new.index("Статус задач")
    assert new.startswith("*<https://doc|")


def test_inject_recap_is_idempotent_replaces_not_duplicates():
    line = "На прошлой встрече: Обсудили раунд."
    once = _inject_recap(_POSTED_NO_RECAP, line)
    twice = _inject_recap(once, "На прошлой встрече: Новый текст.")
    assert twice.count("На прошлой встрече:") == 1
    assert "Новый текст." in twice and "Обсудили раунд." not in twice


def test_inject_recap_preserves_participants_and_status():
    new = _inject_recap(_POSTED_NO_RECAP, "На прошлой встрече: X.")
    assert "Участники: Артем Соколов, Alina Kolpakova" in new
    assert new.rstrip().endswith("Статус задач к обсуждению:")


def test_inject_handles_missing_participants_and_status():
    minimal = "*header only*"
    new = _inject_recap(minimal, "На прошлой встрече: Y.")
    assert new.startswith("*header only*")
    assert "На прошлой встрече: Y." in new


def test_render_recap_line_formats_like_renderer():
    # single sentence → trailing period, label prefix once
    assert _render_recap_line("Обсудили раунд") == "На прошлой встрече: Обсудили раунд."
    # already-terminated → no double period
    assert _render_recap_line("Готово.") == "На прошлой встрече: Готово."
    # empty → empty (no bare label)
    assert _render_recap_line("   ") == ""
