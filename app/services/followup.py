"""Conversational follow-up on action drafts.

When the user @mentions the bot but omits required fields (e.g. due date),
the bot posts the draft card AND asks the missing field in the same thread.
If the user answers in the thread, we parse the reply and update the draft
+ the card via ``chat.update``.

Two parsing paths:
1. LLM multi-field extractor — preferred when a backend is available,
   handles answers like "на пашу до завтра" that cover several fields at
   once.
2. Per-field regex/dateparser fallback for deterministic tests and for
   deployments running without an LLM.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from app.config import Settings, get_settings
from app.logging_setup import get_logger
from app.services.owners import resolve_owner_hint

log = get_logger(__name__)


# Ordered: fields we ask the user about, first match wins.
# Owner is asked before due_date so the passive path (draft card with
# both missing) asks the same first question as the @mention path
# (task auto-created with owner_assumed fallback). The two flows match
# step-by-step, differing only in whether the entity is already a Task.
TASK_FIELD_ORDER = ("title", "owner", "due_date")
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
    # "owner" is virtual — treat it as filled only when we have a real
    # slack_user_id AND the owner wasn't just a fallback to the message
    # author. An owner_assumed flag (set by create_task_from_draft when
    # nobody was explicitly assigned) means the human still owes us an
    # answer, so keep asking.
    if field == "owner":
        if payload.get("owner_assumed"):
            return True
        return not payload.get("owner_user_id")
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


def prompt_for(field: str, *, payload: dict[str, Any] | None = None,
               allowed_owners: list[dict[str, str]] | None = None) -> str:
    """Return the user-facing question for the given field. For 'owner', if
    the LLM extracted a display_name we couldn't resolve, mention that name
    explicitly and list the allowed candidates."""
    base = PROMPTS.get(field, f"Пожалуйста, уточни: {field}")
    if field == "owner" and payload and allowed_owners:
        unresolved = payload.get("owner_display_name")
        if unresolved and not payload.get("owner_user_id"):
            names = ", ".join(o["display_name"] for o in allowed_owners) or "пусто"
            return (
                f"Не нашёл *{unresolved}* в списке. Кому назначаем? "
                f"Доступные: {names}."
            )
    return base


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


# --------------------------------------------------------------------------- #
# LLM-based multi-field extractor (handles 'на пашу до завтра' in one shot)
# --------------------------------------------------------------------------- #


REPLY_TOOL_NAME = "extract_reply_fields"
REPLY_TOOL_DESCRIPTION = (
    "Extract any task/meeting fields the user mentioned in a free-text "
    "reply to the bot's follow-up question."
)
REPLY_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "owner_user_id": {
            "type": "string",
            "description": "Slack user id (U…) from the allowed list.",
        },
        "owner_display_name": {
            "type": "string",
            "description": "Display name from the allowed list.",
        },
        "due_date": {"type": "string", "description": "YYYY-MM-DD"},
        "datetime_at": {
            "type": "string",
            "description": "ISO 8601 with offset if known.",
        },
        "participants": {"type": "array", "items": {"type": "string"}},
        "priority": {
            "type": "string",
            "enum": ["low", "medium", "high", "urgent"],
        },
    },
}


def _reply_system_prompt() -> str:
    return (
        "You are extracting task fields from a user's free-text reply to a "
        "Slack bot's follow-up question. Extract ONLY fields the user "
        "clearly mentioned. Never hallucinate. If owner is mentioned by "
        "name, match against the allowed list and emit the corresponding "
        "slack_user_id AND display_name. For dates: resolve relative "
        "phrases like 'завтра', 'до пятницы', 'на следующей неделе' using "
        "the current_date provided. Return YYYY-MM-DD for due_date and "
        "ISO 8601 for datetime_at."
    )


def _build_reply_user_prompt(
    *,
    reply_text: str,
    awaiting_field: str | None,
    allowed_owners: list[dict[str, str]],
    today: date,
) -> str:
    lines = [
        f"current_date: {today.isoformat()}",
        f"awaiting_field: {awaiting_field or 'any'}",
        "",
        "allowed_owners:",
    ]
    for o in allowed_owners:
        lines.append(f"- {o['display_name']} ({o['slack_user_id']})")
    if not allowed_owners:
        lines.append("- (empty)")
    lines.append("")
    lines.append("user_reply:")
    lines.append(reply_text)
    return "\n".join(lines)


def llm_extract_reply_fields(
    *,
    backend: Any,
    reply_text: str,
    awaiting_field: str | None,
    allowed_owners: list[dict[str, str]],
    today: date | None = None,
) -> dict[str, Any]:
    """Call the LLM to pull all mentioned fields out of a free-text reply.

    Returns {} if the backend refused or raised. Never fills fields that
    weren't mentioned; empty strings are dropped.
    """
    if backend is None or not reply_text.strip():
        return {}
    today = today or date.today()
    user_prompt = _build_reply_user_prompt(
        reply_text=reply_text,
        awaiting_field=awaiting_field,
        allowed_owners=allowed_owners,
        today=today,
    )
    try:
        raw = backend.call_tool(
            system_prompt=_reply_system_prompt(),
            user_prompt=user_prompt,
            tool_name=REPLY_TOOL_NAME,
            tool_description=REPLY_TOOL_DESCRIPTION,
            tool_parameters=REPLY_TOOL_PARAMETERS,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("llm_reply_extract_failed", error=str(e))
        return {}
    if not raw:
        return {}

    cleaned: dict[str, Any] = {}
    for k, v in raw.items():
        if v in (None, "", []):
            continue
        cleaned[k] = v

    # Owner reconciliation:
    # 1) If LLM returned an owner_user_id that's not in the allowed list,
    #    drop the id but KEEP display_name so the bot can hint at the
    #    user that we saw a name but couldn't resolve it.
    # 2) If only display_name came back, try to resolve it locally so
    #    "Иван", "@Ivan" etc. land owner_user_id even without the LLM.
    allowed_ids = {o["slack_user_id"] for o in allowed_owners}
    owner_id = cleaned.get("owner_user_id")
    owner_name = cleaned.get("owner_display_name")
    if owner_id and owner_id not in allowed_ids:
        cleaned.pop("owner_user_id", None)
    if not cleaned.get("owner_user_id") and owner_name:
        match = resolve_owner_hint(
            hint_text=owner_name, allowed_owners=allowed_owners
        )
        if match is not None:
            cleaned["owner_user_id"] = match["slack_user_id"]
            cleaned["owner_display_name"] = match["display_name"]

    return cleaned


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
