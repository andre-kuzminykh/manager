"""FR-CR-05-189b — «TODO:» trailer rule for parent messages.

Operator-pinned 2026-05-21: «если их нет не надо писать TODO».
The parent message must NEVER contain a trailing «TODO:» when the
thread reply would be empty. The trailer is a contract: it signals
to the reader «look in the thread below for tasks». Lying about
it (showing «TODO:» with no thread) is a regression.

These tests ID-to-ID lock the invariant: every modification to
`send_one_fireflies` / `send_one_zoom` MUST keep `build_parent_raw`
in sync, and the contract is enforced here.
"""
from __future__ import annotations

from ops._send_helpers import build_parent_raw


# --------------------------------------------------------------------------- #
# FR-CR-05-189b — «TODO:» trailer
# --------------------------------------------------------------------------- #


def test_fr_cr_05_189b_empty_tasks_omits_todo_trailer() -> None:
    """Parent body MUST NOT have a trailing «TODO:» when there's
    nothing to put in the thread."""
    body = "Заголовок — body of the summary"
    out = build_parent_raw(body, tasks_text="")
    assert out == body
    assert "TODO:" not in out


def test_fr_cr_05_189b_none_tasks_omits_todo_trailer() -> None:
    """`None` tasks_text behaves the same as empty — no trailer."""
    body = "body"
    out = build_parent_raw(body, tasks_text=None)
    assert out == body
    assert "TODO:" not in out


def test_fr_cr_05_189b_with_tasks_adds_todo_trailer() -> None:
    """When a thread reply will follow with tasks, the parent body
    MUST end with `\\n\\nTODO:` so the reader knows to scroll."""
    body = "body"
    tasks = "1) Task — Owner • 19.05.2026 18:00"
    out = build_parent_raw(body, tasks_text=tasks)
    assert out.endswith("\n\nTODO:")
    assert out == "body\n\nTODO:"


def test_fr_cr_05_189b_trailer_is_exact_format() -> None:
    """The trailer is exactly two newlines + «TODO:» — no extra
    whitespace, no different separator, no emojis. Locks the
    operator-pinned format (no emojis per FR-CR-05-184)."""
    body = "x"
    out = build_parent_raw(body, tasks_text="any")
    suffix = out[len(body):]
    assert suffix == "\n\nTODO:"


def test_fr_cr_05_189b_body_passthrough_with_multiline() -> None:
    """Multi-line body is preserved exactly when no tasks; trailer
    added cleanly when tasks present."""
    body = "Line 1\nLine 2\n\nLine 4"
    assert build_parent_raw(body, "") == body
    assert build_parent_raw(body, "tasks") == body + "\n\nTODO:"
