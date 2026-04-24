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

# Russian month stems (match both nominative and genitive: "май"/"мая",
# "июнь"/"июня", …). Keyed by Python month number.
_RU_MONTHS: dict[str, int] = {
    "янв": 1,
    "фев": 2,
    "мар": 3,
    "апр": 4,
    "ма[йя]": 5,
    "июн": 6,
    "июл": 7,
    "авг": 8,
    "сент": 9,
    "окт": 10,
    "нояб": 11,
    "дек": 12,
}

_EN_MONTHS: dict[str, int] = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
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


def _next_day_month(today: date, day: int, month: int) -> date | None:
    """Return the next occurrence of (day, month) strictly after today
    (i.e. same year, or next year if that date has already passed)."""
    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if candidate > today:
            return candidate
    return None


def _try_day_month(lo: str, today: date) -> date | None:
    """Detect "1 мая", "5 июня", "May 1", "by Jun 15" patterns."""
    # Russian: "<day> <month-stem>" ("1 мая", "5 июня").
    for stem, month in _RU_MONTHS.items():
        m = re.search(rf"\b(\d{{1,2}})\s+{stem}\w*", lo)
        if m:
            d = _next_day_month(today, day=int(m.group(1)), month=month)
            if d is not None:
                return d
    # English: "<month> <day>" ("May 1", "Jun 15th").
    for stem, month in _EN_MONTHS.items():
        m = re.search(rf"\b{stem}\w*\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", lo)
        if m:
            d = _next_day_month(today, day=int(m.group(1)), month=month)
            if d is not None:
                return d
    return None


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

    # "N <month>" / "<month> N" — "к 1 мая", "by May 5".
    d = _try_day_month(lo, today)
    if d is not None:
        return d

    # Weekday names (Russian / English), optionally preceded by a preposition.
    for stem, idx in _RU_WEEKDAYS.items():
        if re.search(rf"\b{stem}\w*", lo):
            return _next_weekday(today, idx)
    for name, idx in _EN_WEEKDAYS.items():
        if re.search(rf"\b{name}\b", lo):
            return _next_weekday(today, idx)

    return None


def strip_date_phrase(text: str) -> str:
    """Return `text` with the first date-like phrase removed.

    Used by the title pipeline to drop things like "к 1 мая" / "ко
    вторнику" / "by Friday" from the task title so they end up only
    in the due_date field.

    Handles the same phrase shapes the resolver understands, plus the
    leading prepositions ("к", "до", "на", "by", "to", "on") and the
    ISO date.
    """
    if not text:
        return text

    # Prepositions that commonly precede a date phrase. We accept the
    # preposition optionally so we strip it along with the date.
    _RU_PREPS = r"(?:к(?:о)?|до|на|ко\s)"
    _EN_PREPS = r"(?:by|to|on|before)"

    # Build patterns in a priority order (most specific first).
    patterns: list[str] = []

    # ISO date with optional preposition.
    patterns.append(rf"(?:\b{_RU_PREPS}\s+|\b{_EN_PREPS}\s+)?\d{{4}}-\d{{2}}-\d{{2}}\b")

    # Ru month-name with optional day: "1 мая", "к 25 декабря".
    ru_month_group = "|".join(_RU_MONTHS.keys())
    patterns.append(
        rf"(?:\b{_RU_PREPS}\s+)?\d{{1,2}}\s+(?:{ru_month_group})\w*"
    )

    # En month-name with optional day: "May 1", "by Jun 15th".
    en_month_group = "|".join(_EN_MONTHS.keys())
    patterns.append(
        rf"(?:\b{_EN_PREPS}\s+)?(?:{en_month_group})\w*\s+\d{{1,2}}(?:st|nd|rd|th)?\b"
    )

    # Relative phrases.
    patterns.append(rf"\b{_RU_PREPS}\s+(?:сегодня|завтра|послезавтра)\b")
    patterns.append(r"\b(?:сегодня|завтра|послезавтра)\b")
    patterns.append(rf"\b{_EN_PREPS}\s+(?:today|tomorrow)\b")
    patterns.append(r"\b(?:today|tomorrow)\b")
    patterns.append(r"к\s+концу\s+недели\b")
    patterns.append(r"end of (?:the )?week\b")
    patterns.append(r"на\s+следующей\s+недел[еию]\b")
    patterns.append(r"next\s+week\b")

    # Weekday names with optional preposition.
    ru_wd_group = "|".join(_RU_WEEKDAYS.keys())
    patterns.append(rf"(?:\b{_RU_PREPS}\s+)?(?:{ru_wd_group})\w*")
    en_wd_group = "|".join(_EN_WEEKDAYS.keys())
    patterns.append(rf"(?:\b{_EN_PREPS}\s+)?(?:{en_wd_group})\b")

    out = text
    for pat in patterns:
        new = re.sub(pat, "", out, count=1, flags=re.IGNORECASE)
        if new != out:
            out = new
            break

    # Tidy up leftover whitespace and trailing punctuation.
    out = re.sub(r"\s{2,}", " ", out).strip(" ,.;:—-")
    return out


__all__ = ["resolve_due_date", "strip_date_phrase"]
