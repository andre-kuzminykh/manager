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

# Numeric formats commonly used in Russian / European contexts:
#   DD.MM.YYYY · DD.MM.YY · DD.MM · DD/MM/YYYY · DD/MM
_NUMERIC_DATE_RE = re.compile(
    r"\b(?P<d>\d{1,2})[./](?P<m>\d{1,2})(?:[./](?P<y>\d{2,4}))?\b"
)


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


def _last_day_of_month(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    first_of_next = date(year, month + 1, 1)
    return first_of_next - timedelta(days=1)


def _first_day_of_next_month(today: date) -> date:
    if today.month == 12:
        return date(today.year + 1, 1, 1)
    return date(today.year, today.month + 1, 1)


def _try_numeric_date(text: str, today: date) -> date | None:
    """Handle DD.MM.YYYY · DD.MM.YY · DD.MM · DD/MM · DD/MM/YYYY.

    Bare DD.MM (no year): choose the next future occurrence — i.e. this
    year if the date is still ahead, next year otherwise. Matches the
    day-month logic for named months."""
    for m in _NUMERIC_DATE_RE.finditer(text):
        day = int(m.group("d"))
        month = int(m.group("m"))
        year_raw = m.group("y")
        if not (1 <= day <= 31 and 1 <= month <= 12):
            continue
        if year_raw is not None:
            year = int(year_raw)
            if year < 100:
                year += 2000
            try:
                return date(year, month, day)
            except ValueError:
                continue
        # No year given — pick the next future occurrence.
        return _next_day_month(today, day=day, month=month)
    return None


def _try_month_boundary(lo: str, today: date) -> date | None:
    if re.search(r"к концу месяца|end of (?:the )?month", lo):
        return _last_day_of_month(today.year, today.month)
    if re.search(r"в начале следующего месяца|beginning of next month", lo):
        return _first_day_of_next_month(today)
    if re.search(r"к концу года|end of (?:the )?year", lo):
        return date(today.year, 12, 31)
    if re.search(r"в этом месяце|this month", lo):
        return _last_day_of_month(today.year, today.month)
    return None


# Bare Russian month names (no day): "к маю", "в июне", "by May".
_RU_MONTH_ALONE = {
    "январ": 1,
    "феврал": 2,
    "март": 3,
    "апрел": 4,
    "ма[йеяю]": 5,  # май / мая / мае / маю
    "июн": 6,
    "июл": 7,
    "август": 8,
    "сентябр": 9,
    "октябр": 10,
    "ноябр": 11,
    "декабр": 12,
}


def _try_month_alone(lo: str, today: date) -> date | None:
    """'к маю', 'в июне', 'by May' with no day — resolve to 1st of the
    next future occurrence of that month."""
    # Russian with a preposition so we don't accidentally strip a
    # surname that happens to be the stem.
    for stem, month in _RU_MONTH_ALONE.items():
        if re.search(rf"\b(?:к|до|в)\s+{stem}\w*\b", lo):
            d = _next_day_month(today, day=1, month=month)
            if d is not None:
                return d
    for stem, month in _EN_MONTHS.items():
        if re.search(rf"\bby\s+{stem}\w*\b", lo):
            d = _next_day_month(today, day=1, month=month)
            if d is not None:
                return d
    return None


def _try_in_n_units(lo: str, today: date) -> date | None:
    """Handle "через N дней / недель / месяцев" and "in N days / weeks / months".

    A missing number means 1: "через неделю" → +7 days.
    "через пару" / "a couple of" counts as N=2.
    """
    # Russian: "через пару <unit>".
    m = re.search(r"\bчерез\s+пар[уы]\s+(день|дн[яей]|недел\w+|месяц\w*)", lo)
    if m:
        unit = m.group(1)
        if unit.startswith("дн") or unit == "день":
            return today + timedelta(days=2)
        if unit.startswith("недел"):
            return today + timedelta(days=14)
        if unit.startswith("месяц"):
            return today + timedelta(days=60)

    # Russian: "через [N] <unit>". N is optional.
    m = re.search(
        r"\bчерез\s+(?:(\d+)\s+)?(день|дн[яей]|недел\w+|месяц\w*|год\w*)",
        lo,
    )
    if m:
        n = int(m.group(1)) if m.group(1) else 1
        unit = m.group(2)
        if unit.startswith("дн") or unit == "день":
            return today + timedelta(days=n)
        if unit.startswith("недел"):
            return today + timedelta(days=7 * n)
        if unit.startswith("месяц"):
            return today + timedelta(days=30 * n)
        if unit.startswith("год"):
            return today + timedelta(days=365 * n)

    # English "a couple of <unit>".
    m = re.search(r"\ba couple of\s+(day|week|month|year)s?\b", lo)
    if m:
        unit = m.group(1)
        return today + timedelta(
            days={"day": 2, "week": 14, "month": 60, "year": 2 * 365}[unit]
        )

    # English: "in [N] day/week/month/year(s)". "in a week" = 1 week.
    m = re.search(
        r"\bin\s+(?:(\d+)|a|an)\s+(day|week|month|year)s?\b",
        lo,
    )
    if m:
        n = int(m.group(1)) if m.group(1) else 1
        unit = m.group(2)
        if unit == "day":
            return today + timedelta(days=n)
        if unit == "week":
            return today + timedelta(days=7 * n)
        if unit == "month":
            return today + timedelta(days=30 * n)
        if unit == "year":
            return today + timedelta(days=365 * n)
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

    # Numeric formats: DD.MM.YYYY · DD/MM · etc.
    d = _try_numeric_date(text, today)
    if d is not None:
        return d

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

    # "через N <unit>" / "in N <unit>" — "через неделю", "через 2 дня",
    # "in 3 weeks", "in a month".
    d = _try_in_n_units(lo, today)
    if d is not None:
        return d

    # Month boundaries ("к концу месяца", "end of year").
    d = _try_month_boundary(lo, today)
    if d is not None:
        return d

    # "на этой неделе" / "this week" → Friday of the current week.
    if re.search(r"на этой недел[еию]|this week\b", lo):
        return _next_weekday(today, 4)

    # "N <month>" / "<month> N" — "к 1 мая", "by May 5". Must run
    # before _try_month_alone so "by May 5" doesn't resolve to 1 May.
    d = _try_day_month(lo, today)
    if d is not None:
        return d

    # Bare month names ("к маю", "by May"), no day.
    d = _try_month_alone(lo, today)
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

    # "через N <unit>" / "in N <unit>" / "через пару <unit>" / "a couple
    # of <unit>". Permissive: optional adverb ("ровно", "примерно",
    # "около", "exactly", "about", "around") plus up to 4 filler words
    # between "через"/"in" and the unit, so we eat "через неделю",
    # "через три недели", "через ровно пять недель", "in a couple of
    # weeks", "in about two months".
    patterns.append(
        r"\b(?:ровно\s+|примерно\s+|около\s+)?через(?:\s+\w+){0,4}?\s+(?:день|дн[яей]|недел\w+|месяц\w*|год\w*)\w*"
    )
    patterns.append(
        r"\b(?:exactly\s+|about\s+|around\s+|roughly\s+)?(?:in|within)(?:\s+\w+){0,4}?\s+(?:day|week|month|year)s?\b"
    )

    # Numeric date formats, with optional leading preposition.
    patterns.append(
        rf"(?:\b{_RU_PREPS}\s+|\b{_EN_PREPS}\s+)?\d{{1,2}}[./]\d{{1,2}}(?:[./]\d{{2,4}})?\b"
    )

    # Month boundaries.
    patterns.append(r"к концу месяца|end of (?:the )?month")
    patterns.append(r"к концу года|end of (?:the )?year")
    patterns.append(r"в этом месяце|this month")
    patterns.append(r"в начале следующего месяца|beginning of next month")

    # Bare month names with prepositions.
    ru_month_alone_group = "|".join(_RU_MONTH_ALONE.keys())
    patterns.append(rf"\b(?:к|до|в)\s+(?:{ru_month_alone_group})\w*\b")
    en_month_alone_group = "|".join(_EN_MONTHS.keys())
    patterns.append(rf"\bby\s+(?:{en_month_alone_group})\w*\b")

    # "на этой неделе" / "this week".
    patterns.append(r"\bна этой недел[еию]\b")
    patterns.append(r"\bthis week\b")

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
