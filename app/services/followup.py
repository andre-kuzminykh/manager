"""Conversational follow-up on action drafts.

When the user @mentions the bot but omits required fields (e.g. due date),
the bot posts the draft card AND asks the missing field in the same thread.
If the user answers in the thread, we parse the reply and update the draft
+ the card via ``chat.update``.

Field parser currently supports:
- due_date / datetime_at — free-text date via dateparser (ru + en)
- owner — resolved against ALLOWED_OWNERS via resolve_owner_hint
- title / description / notes — free-text (trimmed)
- participants — comma-separated list
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from app.config import Settings
from app.services.owners import resolve_owner_hint


# Ordered: fields we ask the user about, first match wins.
TASK_FIELD_ORDER = ("title", "due_date", "owner")
MEETING_FIELD_ORDER = ("title", "datetime_at", "participants")

# Human-friendly prompt per field.
PROMPTS: dict[str, str] = {
    "due_date": "Какой дедлайн? Можно написать просто: `до пятницы`, `завтра`, `2026-05-01`.",
    "datetime_at": "На какое число и время? Например: `завтра в 15:00`, `2026-05-01 10:00`.",
    "owner": "Кому назначаем? Укажи имя из списка или упомяни через `@`.",
    "title": "Как сформулировать задачу в одну строку?",
    "participants": "Кто участники? Перечисли через запятую.",
    "description": "Добавим описание? Можешь ответить в треде.",
    "notes": "Какие заметки приложить? Можешь ответить в треде.",
}


# Normalised empty check for each field (payload is a dict).
def _is_empty(field: str, payload: dict[str, Any]) -> bool:
    # "owner" is virtual — back it by owner_user_id / owner_display_name.
    if field == "owner":
        return not (payload.get("owner_user_id") or payload.get("owner_display_name"))
    value = payload.get(field)
    if value is None or value == "":
        return True
    if field == "participants" and isinstance(value, list) and not value:
        return True
    return False


def pick_next_missing(intent: str, payload: dict[str, Any]) -> str | None:
    """Return the next field to ask about, or None if everything we care
    about is filled."""
    order = TASK_FIELD_ORDER if intent.endswith("task") else MEETING_FIELD_ORDER
    for field in order:
        if _is_empty(field, payload):
            return field
    return None


def prompt_for(field: str) -> str:
    return PROMPTS.get(field, f"Пожалуйста, уточни: {field}")


# --------------------------------------------------------------------------- #
# Parsing free-text replies
# --------------------------------------------------------------------------- #


_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")


def parse_reply(
    *,
    field: str,
    reply_text: str,
    settings: Settings,
    today: date | None = None,
) -> dict[str, Any] | None:
    """Parse a user's thread reply for the awaited field.

    Returns a dict of fields to merge into the draft payload, or None if we
    could not extract anything useful.
    """
    reply_text = (reply_text or "").strip()
    if not reply_text:
        return None

    if field == "due_date":
        parsed = _parse_date(reply_text, today=today)
        if parsed is None:
            return None
        return {"due_date": parsed.isoformat()}

    if field == "datetime_at":
        parsed_dt = _parse_datetime(reply_text, today=today)
        if parsed_dt is None:
            return None
        return {"datetime_at": parsed_dt.isoformat()}

    if field == "owner":
        owner = resolve_owner_hint(
            hint_text=reply_text, allowed_owners=settings.allowed_owners()
        )
        if owner is None:
            return None
        return {
            "owner_user_id": owner["slack_user_id"],
            "owner_display_name": owner["display_name"],
        }

    if field == "title":
        # Strip any lingering bot mention tokens.
        cleaned = _MENTION_RE.sub("", reply_text).strip()
        if not cleaned:
            return None
        return {"title": cleaned}

    if field == "participants":
        parts = [p.strip() for p in re.split(r"[,\n]", reply_text) if p.strip()]
        if not parts:
            return None
        return {"participants": parts}

    if field in ("description", "notes"):
        return {field: reply_text}

    return None


_STOPWORDS_RU = re.compile(
    r"^\s*(до|к|на|по|не\s+позже|не\s+позднее)\s+", re.IGNORECASE
)

# Russian genitive → nominative day names (dateparser only handles nominative).
_RU_DAY_GENITIVE = {
    "понедельника": "понедельник",
    "вторника": "вторник",
    "среды": "среда",
    "четверга": "четверг",
    "пятницы": "пятница",
    "субботы": "суббота",
    "воскресенья": "воскресенье",
}


def _strip_preposition(text: str) -> str:
    """Drop leading 'до', 'к', 'на' etc. and normalise day names so dateparser
    can handle phrases like 'до пятницы' → 'пятница'."""
    without = _STOPWORDS_RU.sub("", text).strip()
    lower = without.lower()
    for gen, nom in _RU_DAY_GENITIVE.items():
        if lower == gen:
            return nom
    return without


def _parse_date(text: str, *, today: date | None = None) -> date | None:
    # 1) ISO YYYY-MM-DD short-circuit.
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        pass

    try:
        import dateparser
    except ImportError:  # pragma: no cover
        return None
    today = today or date.today()
    settings = {
        "PREFER_DATES_FROM": "future",
        "RELATIVE_BASE": datetime.combine(today, datetime.min.time()),
    }
    for candidate in (text, _strip_preposition(text)):
        dt = dateparser.parse(candidate, languages=["ru", "en"], settings=settings)
        if dt is not None:
            return dt.date()
    return None


def _parse_datetime(text: str, *, today: date | None = None) -> datetime | None:
    # 1) ISO 8601 short-circuit.
    try:
        return datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        pass

    try:
        import dateparser
    except ImportError:  # pragma: no cover
        return None
    today = today or date.today()
    settings = {
        "PREFER_DATES_FROM": "future",
        "RELATIVE_BASE": datetime.combine(today, datetime.min.time()),
        "RETURN_AS_TIMEZONE_AWARE": False,
    }
    for candidate in (text, _strip_preposition(text)):
        dt = dateparser.parse(candidate, languages=["ru", "en"], settings=settings)
        if dt is not None:
            return dt
    return None
