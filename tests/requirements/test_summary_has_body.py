"""FR-CR-05-160 — `summary_has_body` content-quality gate.

Regression 2026-06-03: Whisper hallucination / silent audio / fragmented
VTT produced meeting summaries that collapsed to just the title +
«Участники: …» line + whitespace. Those reached BOTH the operator's
Slack AND the external n8n webhook as junk («Design Status», «SDF <>
Humanoid», «Алина, Ирина»). This gate returns False for that shape so
the publish paths can suppress it.
"""
from __future__ import annotations

from app.services.transcription import summary_has_body


# The exact content-free shape that leaked to the webhook on 2026-06-03.
_EMPTY = (
    "Алина, Ирина  — 03.06.2026 | 30 мин\n\n"
    "03/06 - Алина, Ирина\n\n"
    "Участники: Артем Соколов, Alina Kolpakova, Ирина Шипилова\n\n\n\n"
)

_REAL = (
    "Алина, Ирина  — 03.06.2026 | 30 мин\n\n"
    "03/06 - Алина, Ирина\n\n"
    "Участники: Артем Соколов, Alina Kolpakova\n\n"
    "Суть: Обсудили статус фандрайзинга по Nvidia, зафиксировали "
    "целевые объёмы и распределили follow-up по инвесторам Mirae и "
    "Mubadala.\n\n"
    "To-Do:\n1) Отправить апдейт — Alina • 04.06.2026\n"
    "2) Уточнить чек — Артем • 05.06.2026\n"
)


def test_empty_body_is_not_publishable() -> None:
    assert summary_has_body(_EMPTY) is False


def test_real_summary_is_publishable() -> None:
    assert summary_has_body(_REAL) is True


def test_none_and_blank_are_not_publishable() -> None:
    assert summary_has_body(None) is False
    assert summary_has_body("") is False
    assert summary_has_body("   \n\n  ") is False


def test_participants_only_not_publishable() -> None:
    s = "Weekly sync — 03.06.2026\n\nУчастники: A, B, C\n"
    assert summary_has_body(s) is False


def test_header_and_participants_stripped_before_measuring() -> None:
    # Even a long participant line must not count as body.
    s = (
        "Title — 03.06.2026 | 60 мин\n\n"
        "03/06 - Title\n\n"
        "Участники: " + ", ".join(f"Person {i}" for i in range(40)) + "\n"
    )
    assert summary_has_body(s) is False


def test_short_real_body_above_threshold() -> None:
    s = (
        "Title — 03.06.2026\n\n"
        "Участники: A\n\n"
        "Суть: Договорились перенести релиз на следующую неделю и "
        "согласовать бюджет с финансовым отделом до пятницы.\n"
    )
    assert summary_has_body(s) is True


def test_threshold_is_tunable() -> None:
    s = "Title — 03.06.2026\n\nУчастники: A\n\nСуть: коротко.\n"
    # Default threshold rejects a near-empty body…
    assert summary_has_body(s) is False
    # …but a low threshold accepts it.
    assert summary_has_body(s, min_body_chars=5) is True


__all__: list[str] = []
