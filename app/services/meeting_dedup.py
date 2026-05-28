"""FR-CR-05-203 — cross-source Zoom↔Fireflies meeting dedup.

A meeting can be recorded simultaneously by Zoom Cloud AND the Fireflies
bot, producing two independent rows (`zoom_recordings` + `meeting_recordings`)
for the SAME meeting. Per-row idempotency (`slack_post_ts`, FR-CR-05-194)
can't catch this because the rows are distinct. This module lets each
pipeline skip the *second* capture entirely when the other source has
already posted the meeting to Slack.

Matching is by normalized title + meeting_date proximity (the two captures
start within a couple of minutes; Fireflies prepends a «DD/MM - » prefix
that we strip before comparing).
"""
from __future__ import annotations

import re
import unicodedata
from datetime import timedelta
from typing import Any

from app.logging_setup import get_logger

log = get_logger(__name__)

# Leading date prefix Fireflies adds to the title: «27/05 - », «27/05/2026 - ».
_DATE_PREFIX = re.compile(r"^\s*\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?\s*[-—–]\s*")


def normalize_meeting_title(title: str | None) -> str:
    """Comparable key for a meeting title: strip a leading «DD/MM - »
    date prefix, NFKD-normalize, drop combining marks, lowercase, collapse
    whitespace. So «27/05 - Weekly Top Management meeting» and
    «Weekly Top Management meeting» compare equal."""
    if not title:
        return ""
    s = _DATE_PREFIX.sub("", title)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def find_cross_source_duplicate(
    session: Any,
    *,
    title: str | None,
    meeting_date: Any,
    self_kind: str,
    self_id: str,
    window_minutes: int = 30,
    time_window_minutes: int = 10,
) -> tuple[str, str] | None:
    """Return ``(kind, id)`` of another recording (in either
    ``zoom_recordings`` or ``meeting_recordings``) that represents the same
    meeting and is already posted to Slack (``slack_post_ts IS NOT NULL``),
    excluding the row itself. Returns ``None`` when no such duplicate exists.

    Two match paths:
      1. **Title** — normalized title equal AND ``meeting_date`` within
         ``±window_minutes`` (any source, incl. same-source re-records).
      2. **FR-CR-05-209 time-only** — Fireflies RENAMES meetings, so the same
         meeting captured by Zoom and Fireflies has DIFFERENT titles
         («Kodai Yamagishi … Zoom call» vs «Mitsubishi: роботы…»). A person is
         in one meeting at a time, so the CLOSEST already-posted recording in
         the OTHER source within a tight ``±time_window_minutes`` is the same
         meeting. Restricted to cross-source (``kind != self_kind``) to avoid
         skipping a legitimate same-source re-record.

    ``self_kind`` ∈ {"zoom", "fireflies"}.

    Defensive: any error is swallowed and ``None`` returned — a dedup-check
    failure must never block legitimate processing.
    """
    try:
        from sqlalchemy import text

        if meeting_date is None:
            return None
        key = normalize_meeting_title(title)
        lo = meeting_date - timedelta(minutes=window_minutes)
        hi = meeting_date + timedelta(minutes=window_minutes)
        time_best: tuple[timedelta, str, str] | None = None
        for table, col, kind in (
            ("zoom_recordings", "zoom_id", "zoom"),
            ("meeting_recordings", "fireflies_id", "fireflies"),
        ):
            rows = session.execute(
                text(
                    f"SELECT {col} AS id, title, meeting_date AS md FROM {table} "
                    f"WHERE meeting_date BETWEEN :lo AND :hi "
                    f"AND slack_post_ts IS NOT NULL"
                ),
                {"lo": lo, "hi": hi},
            )
            for r in rows:
                rid = r.id if hasattr(r, "id") else r[0]
                rtitle = r.title if hasattr(r, "title") else r[1]
                if kind == self_kind and rid == self_id:
                    continue  # don't treat the row's own post as a duplicate
                # 1. Strong: normalized title match.
                if key and normalize_meeting_title(rtitle) == key:
                    return (kind, rid)
                # 2. Weaker: cross-source time proximity (renamed meeting).
                rmd = getattr(r, "md", None)
                if kind != self_kind and rmd is not None:
                    delta = abs(rmd - meeting_date)
                    if delta <= timedelta(minutes=time_window_minutes) and (
                        time_best is None or delta < time_best[0]
                    ):
                        time_best = (delta, kind, rid)
        if time_best is not None:
            return (time_best[1], time_best[2])
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("meeting_dedup_check_failed", error=str(e))
        return None


__all__ = ["normalize_meeting_title", "find_cross_source_duplicate"]
