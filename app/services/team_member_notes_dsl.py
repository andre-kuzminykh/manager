"""FR-CR-05-193d-1 — parser DSL маркеров из TeamMember.notes."""
from __future__ import annotations

import re


_DELEGATE_RE = re.compile(
    r"DELEGATE_TASKS_TO\s*:\s*(.+?)(?:[.\n]|$)",
    re.IGNORECASE,
)
_DO_NOT_CALL_RE = re.compile(r"\bDO_NOT_CALL\b", re.IGNORECASE)


def parse_notes_dsl(notes: str | None) -> dict:
    """Extract DELEGATE_TASKS_TO + DO_NOT_CALL markers из freeform notes.

    Returns:
        {"delegate_to": str | None, "do_not_call": bool}
    """
    if not notes or not notes.strip():
        return {"delegate_to": None, "do_not_call": False}

    delegate_to: str | None = None
    m = _DELEGATE_RE.search(notes)
    if m:
        delegate_to = m.group(1).strip()

    do_not_call = bool(_DO_NOT_CALL_RE.search(notes))

    return {"delegate_to": delegate_to, "do_not_call": do_not_call}
