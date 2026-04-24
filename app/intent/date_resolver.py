"""Safety-net date resolver.

gpt-4o-mini routinely ignores the "Weekday lookup" table in the prompt and
leaves due_date null even when the source message says «к понедельнику» or
«завтра». This module resolves those phrases in Python so the Task row
always gets a date when one was clearly named.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

# Russian weekday names → Python weekday index (Monday=0).
_RU_WEEKDAYS = {
    "понедельник": 0,
    "вторник": 1,
    "сред": 2,  # среда / среду / среде
    "четверг": 3,
    "пятниц": 4,  # пятница / пятницу / пятнице
    "суббот": 5,  # суббота / субботу / субботе
    "воскресен": 6,  # воскресенье / воскресенья
}

_EN_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_ISO_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def _next_weekday(today: date, target: int) -> date:
    """Smallest positive offset that lands on `target` (0..6, Mon=0).

    If today is already that weekday we move to NEXT week — matches the
    prompt's +1..+7 table (strictly future)."""
    offset = (target - today.weekday()) % 7
    if offset == 0:
        offset = 7
    return today + timedelta(days=offset)


def resolve_due_date(text: str, today: date) -> date | None:
    """Return the first date mentioned in `text`, resolved against `today`.

    Returns None if nothing clearly date-like is present."""
    if not text:
        return None
    lo = text.lower()

    # Explicit ISO date wins if present.
    m = _ISO_RE.search(text)
    if m:
        try:
            return date.fromisoformat(m.group(1))
        except ValueError:
            pass

    # Relative phrases.
    if re.search(r"\bсегодня\b", lo) or re.search(r"\btoday\b", lo):
        return today
    if re.search(r"\bпослезавтра\b", lo) or re.search(r"\bday after tomorrow\b", lo):
        return today + timedelta(days=2)
    if re.search(r"\bзавтра\b", lo) or re.search(r"\btomorrow\b", lo):
        return today + timedelta(days=1)
    if re.search(r"к концу недели|end of (the )?week", lo):
        return _next_weekday(today, 4)  # Friday
    if re.search(r"на следующей неделе|next week", lo):
        return _next_weekday(today, 0)  # upcoming Monday

    # Weekday names (Russian / English), optionally preceded by a preposition.
    for stem, idx in _RU_WEEKDAYS.items():
        if re.search(rf"\b{stem}\w*", lo):
            return _next_weekday(today, idx)
    for name, idx in _EN_WEEKDAYS.items():
        if re.search(rf"\b{name}\b", lo):
            return _next_weekday(today, idx)

    return None


__all__ = ["resolve_due_date"]
