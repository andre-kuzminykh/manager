"""FR-CR-05-157e — single content gate: an empty/contentless meeting must be
published to NO channel (Telegram + Slack + webhook). Regression: the FF
meeting titled «Запись без содержимого» (0 tasks, prose-but-empty summary)
slipped the phrase-only is_summary_no_content and reached BOTH Slack and the
n8n webhook.
"""
from __future__ import annotations

from app.services.transcription import is_contentless_meeting

# A real summary with an actual prose body (passes summary_has_body).
_REAL = (
    "04/06 - Strategy sync\n"
    "Участники: Anna, Boris\n"
    "Обсудили дорожную карту продукта, согласовали сроки по релизу и "
    "распределили зоны ответственности между командами на следующий квартал."
)


def test_empty_recording_title_ru_suppressed() -> None:
    ok, reason = is_contentless_meeting(
        title="Запись без содержимого", short_summary=_REAL, tasks_count=0)
    assert ok and "empty_recording_title" in reason


def test_empty_recording_title_with_ddmm_prefix() -> None:
    # derived title carries the «DD/MM - » prefix — still caught
    ok, _ = is_contentless_meeting(
        title="04/06 - Запись без содержимого", short_summary=_REAL, tasks_count=2)
    assert ok


def test_empty_recording_title_english() -> None:
    for t in ("Recording without content", "No content", "Empty recording"):
        ok, _ = is_contentless_meeting(title=t, short_summary=_REAL, tasks_count=0)
        assert ok, t


def test_no_content_phrase_in_summary_suppressed() -> None:
    ok, reason = is_contentless_meeting(
        title="04/06 - Sync",
        short_summary="Участники: A\nСодержательная часть встречи не зафиксирована.",
        tasks_count=0)
    assert ok and "summary_no_content" in reason


def test_real_meeting_with_tasks_is_published() -> None:
    ok, reason = is_contentless_meeting(
        title="04/06 - Strategy sync", short_summary=_REAL, tasks_count=3)
    assert ok is False and reason is None


def test_real_meeting_zero_tasks_but_real_body_is_published() -> None:
    # a legit info-sync with no action items but a real recap must NOT be suppressed
    ok, _ = is_contentless_meeting(
        title="04/06 - Strategy sync", short_summary=_REAL, tasks_count=0)
    assert ok is False


__all__: list[str] = []
