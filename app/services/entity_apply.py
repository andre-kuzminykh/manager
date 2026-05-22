"""FR-CR-05-193c — Step 3: deterministic apply.

Применяет mappings от matcher к summary text + tasks. Pure Python, no LLM.
Idempotent.
"""
from __future__ import annotations

import re
from typing import Any

from app.logging_setup import get_logger
from app.services.team_member_notes_dsl import parse_notes_dsl

log = get_logger(__name__)


def apply_text_replacements(
    text: str | None,
    *,
    replacements: list[dict],
) -> str:
    """Regex word-boundary aware replace. Idempotent на already-canonical text.

    Args:
      text: source text (summary_detailed / summary_short)
      replacements: list[{"raw": str, "canonical": str}]
    """
    if not text:
        return ""
    if not replacements:
        return text
    out = text
    # Sort by raw length DESC чтобы long mentions replace ПЕРВЫМИ
    # (избегаем substring сlasses).
    sorted_repls = sorted(replacements, key=lambda r: -len(r.get("raw") or ""))
    for r in sorted_repls:
        raw = (r.get("raw") or "").strip()
        canon = (r.get("canonical") or "").strip()
        if not raw or not canon or raw == canon:
            continue
        # Idempotency check — если canonical уже содержит raw, не зацикливаемся
        # Используем negative lookbehind / lookahead для word boundary
        # works for cyrillic + latin
        pattern = re.compile(
            r"(?<![\wЀ-ӿ])" + re.escape(raw) + r"(?![\wЀ-ӿ])"
        )
        # Idempotency: если canonical уже в тексте на месте raw, skip
        # (regex replace всё равно проверит word boundary)
        out = pattern.sub(canon, out)
    return out


def apply_task_owner(
    task_data: dict,
    *,
    tm_real_name: str | None,
    session: Any,
    matcher_reasoning: str | None = None,
) -> dict:
    """Lookup TeamMember by real_name + apply DELEGATE / DO_NOT_CALL markers.

    Возвращает task_data с заполненными:
      owner_display_name, owner_user_id, matcher_meta.

    Если tm_real_name=None — owner_display_name=None,
    matcher_meta.status='no_match'.
    """
    from app.models import TeamMember
    raw_owner = (task_data.get("raw_owner_mention") or "").strip()

    if not tm_real_name:
        return {
            **task_data,
            "owner_display_name": None,
            "owner_user_id": None,
            "matcher_meta": {
                "raw_owner_mention": raw_owner,
                "tm_id": None,
                "tm_real_name": None,
                "reasoning": matcher_reasoning or "no_match",
                "status": "no_match",
            },
        }

    try:
        tm = session.query(TeamMember).filter_by(real_name=tm_real_name).first()
    except Exception as e:  # noqa: BLE001
        log.warning("apply_task_owner_db_error", error=str(e))
        return {
            **task_data,
            "owner_display_name": None,
            "owner_user_id": None,
            "matcher_meta": {
                "raw_owner_mention": raw_owner,
                "tm_id": None,
                "tm_real_name": None,
                "reasoning": "apply_error",
                "status": "apply_error",
            },
        }

    if tm is None:
        return {
            **task_data,
            "owner_display_name": None,
            "owner_user_id": None,
            "matcher_meta": {
                "raw_owner_mention": raw_owner,
                "tm_id": None,
                "tm_real_name": None,
                "reasoning": "tm_not_found_in_db",
                "status": "no_match",
            },
        }

    dsl = parse_notes_dsl(getattr(tm, "notes", None))

    # DO_NOT_CALL — task без owner
    if dsl["do_not_call"]:
        return {
            **task_data,
            "owner_display_name": None,
            "owner_user_id": None,
            "matcher_meta": {
                "raw_owner_mention": raw_owner,
                "tm_id": getattr(tm, "id", None),
                "tm_real_name": tm_real_name,
                "original_owner": tm_real_name,
                "reasoning": matcher_reasoning or "matched_but_do_not_call",
                "status": "skipped_do_not_call",
            },
        }

    # DELEGATE_TASKS_TO marker
    if dsl["delegate_to"]:
        try:
            target = session.query(TeamMember).filter_by(
                real_name=dsl["delegate_to"]
            ).first()
        except Exception:  # noqa: BLE001
            target = None
        if target is not None:
            return {
                **task_data,
                "owner_display_name": target.real_name,
                "owner_user_id": (
                    getattr(target, "slack_user_id", None)
                    or (str(getattr(target, "telegram_user_id", None))
                        if getattr(target, "telegram_user_id", None) else None)
                ),
                "matcher_meta": {
                    "raw_owner_mention": raw_owner,
                    "tm_id": getattr(target, "id", None),
                    "tm_real_name": target.real_name,
                    "original_owner": tm_real_name,
                    "reasoning": matcher_reasoning or "delegated",
                    "status": "delegated",
                },
            }

    # Plain match
    return {
        **task_data,
        "owner_display_name": tm_real_name,
        "owner_user_id": (
            getattr(tm, "slack_user_id", None)
            or (str(getattr(tm, "telegram_user_id", None))
                if getattr(tm, "telegram_user_id", None) else None)
        ),
        "matcher_meta": {
            "raw_owner_mention": raw_owner,
            "tm_id": getattr(tm, "id", None),
            "tm_real_name": tm_real_name,
            "reasoning": matcher_reasoning or "matched",
            "status": "matched",
        },
    }


def build_task_from_extracted(
    task_data: dict,
    *,
    source_kind: str,
    source_conversation_id: str,
) -> dict:
    """Compose persistable task dict.

    status='proposed' default. status='todo' if matcher_meta.status='delegated'.
    """
    meta = task_data.get("matcher_meta") or {}
    status = "proposed"
    if meta.get("status") == "delegated":
        status = "todo"

    return {
        "title": (task_data.get("title") or "")[:512],
        "description": task_data.get("description"),
        "owner_display_name": task_data.get("owner_display_name"),
        "owner_user_id": task_data.get("owner_user_id"),
        "priority": task_data.get("priority", "medium"),
        "status": status,
        "due_date": task_data.get("due_date"),
        "source_kind": source_kind,
        "source_conversation_id": source_conversation_id,
        "extra": {
            "matcher_meta": meta,
        },
    }
