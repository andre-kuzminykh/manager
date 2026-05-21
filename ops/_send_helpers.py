"""FR-CR-05-189b — shared helpers for `send_one_fireflies` and
`send_one_zoom` so the «TODO:» trailer rule is testable in one place.

Operator-pinned 2026-05-21: «если их нет не надо писать TODO».
A parent message must NEVER contain a trailing «TODO:» line when the
thread reply that follows it would be empty. The trailer is a signal
to the reader «look in the thread for tasks» — appending it without
a thread reply is a lie.
"""
from __future__ import annotations


def build_parent_raw(body: str, tasks_text: str | None) -> str:
    """FR-CR-05-189b — compose the raw parent message body.

    Append the «\n\nTODO:» trailer if and only if ``tasks_text`` is
    truthy (non-empty / non-None). When no tasks will follow, the
    body is returned as-is, with no orphan trailer.

    Invariants:
      - ``build_parent_raw(body, "")`` == ``body``
      - ``build_parent_raw(body, None)`` == ``body``
      - ``build_parent_raw(body, "...tasks...").endswith("TODO:")``
      - The trailer adds exactly two newlines + the literal «TODO:»;
        nothing else.
    """
    if not tasks_text:
        return body
    return body + "\n\nTODO:"


__all__ = ["build_parent_raw"]
