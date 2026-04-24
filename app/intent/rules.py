"""Rule-based prefilter. Cheap, deterministic signal before calling the LLM.

These rules do not commit to any intent; they just produce a coarse hint that
the orchestrator (or the LLM prompt) can combine with a real classification.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.schemas.intent import IntentType

_TASK_KEYWORDS = [
    r"\bзадач[аиуе]",
    r"\btask\b",
    r"\btodo\b",
    r"\bto[-\s]?do\b",
    r"\bсделай\b",
    r"\bнадо\b",
    r"\bнужно\b",
    r"\bтребуется\b",
    r"\bподготов\w+",
    r"\bпочини\w+",
    r"\bисправ\w+",
    r"\bпоправ\w+",
    r"\bсобери\w*|собрать\b",
    r"\bотправ\w+|отошли\b",
    r"\bнапиш\w+|написать\b",
    r"\bдедлайн",
    r"\bdue\b",
    r"\bfix\b",
    r"\bprepare\b",
    r"\bneed to\b",
]

_MEETING_KEYWORDS = [
    r"\bвстреч[аиуе]",
    r"\bmeeting\b",
    r"\bcall\b",
    r"\bзвонок",
    r"\bсозвон",
    r"\bкалендар",
    r"\bcalendar\b",
    r"\bsync\b",
    r"\binterview\b",
]

_UPDATE_KEYWORDS = [
    r"\bобнов\w+",
    r"\bизмени\w+",
    r"\bперенес\w+",
    r"\bupdate\b",
    r"\breschedul\w+",
    r"\bmove to\b",
]

_TASK_RE = re.compile("|".join(_TASK_KEYWORDS), re.IGNORECASE)
_MEETING_RE = re.compile("|".join(_MEETING_KEYWORDS), re.IGNORECASE)
_UPDATE_RE = re.compile("|".join(_UPDATE_KEYWORDS), re.IGNORECASE)


@dataclass
class PrefilterResult:
    hint: IntentType
    score: float  # 0..1, how confident the cheap rules are


def prefilter_intent(text: str) -> PrefilterResult:
    text = (text or "").strip()
    if not text:
        return PrefilterResult(hint=IntentType.no_action, score=0.0)

    has_task = bool(_TASK_RE.search(text))
    has_meeting = bool(_MEETING_RE.search(text))
    has_update = bool(_UPDATE_RE.search(text))

    if has_meeting and has_update:
        return PrefilterResult(hint=IntentType.update_meeting, score=0.6)
    if has_task and has_update:
        return PrefilterResult(hint=IntentType.update_task, score=0.6)
    if has_meeting:
        return PrefilterResult(hint=IntentType.create_meeting, score=0.55)
    if has_task:
        return PrefilterResult(hint=IntentType.create_task, score=0.55)
    return PrefilterResult(hint=IntentType.no_action, score=0.1)
