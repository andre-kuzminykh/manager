"""FR-CR-05-192u — ID-locked tests for the LLM-free agenda compose
path. Operator-pinned 2026-05-22: «никакого LLM, просто `На прошлой
встрече:` и предыдущее саммари как есть».

Contract locked here:
  - `compose_agenda` calls `_compose_lite` exclusively. No LLM
    network call, no `complete_json`, no `chat.completions.create`.
  - `previous_recap` is a single-element list with the body of
    the most-recent prior recording's `short_summary` —
    title `<a href>` wrapper stripped, «Участники: …» line
    stripped, trailing «TODO:» block stripped, HTML entities
    decoded.
  - `open_questions` is always empty (no LLM invents discussion
    items).
  - `tasks_checklist` is a deterministic passthrough of
    `candidate.open_tasks` — title / description / owner / due /
    status reproduced verbatim, no rewrites, no filtering.
  - `doc_body_md` is built deterministically from the same data.

The Slack renderer (`render_agenda_text`) prepends «На прошлой
встрече: » exactly once when it renders the recap — see
`app/agenda/slack_format.py`. Renderer untouched.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.agenda.compose import (
    _compose_lite,
    _strip_prior_short_summary_body,
    compose_agenda,
)
from app.agenda.service import AgendaCandidate


def _candidate(*, prior=None, open_tasks=None) -> AgendaCandidate:
    return AgendaCandidate(
        calendar_event_id="evt-1",
        recurring_event_id=None,
        title="Fundraising daily",
        title_normalised="fundraising daily",
        scheduled_start_at=datetime(2026, 5, 22, 11, 30, tzinfo=timezone.utc),
        description=None,
        attendees=["Артем Соколов", "Дмитрий Седов"],
        prior_recordings=prior or [],
        open_tasks=open_tasks or [],
    )


# --------------------------------------------------------------------------- #
# _strip_prior_short_summary_body — title + Участники + TODO stripped
# --------------------------------------------------------------------------- #


def test_fr_cr_05_192u_strip_removes_title_wrapper() -> None:
    """`<a href="...">DD/MM - Title</a>` first-line wrapper MUST be
    stripped, and HTML entities decoded."""
    s = (
        '<a href="https://docs.google.com/d/x">22/05 - Title</a>\n\n'
        "Участники: A, B\n\n"
        "Recap body.\n"
    )
    assert _strip_prior_short_summary_body(s) == "Recap body."


def test_fr_cr_05_192u_strip_removes_participants_line() -> None:
    """Участники: line MUST be dropped wherever it sits."""
    s = "<a href=\"u\">T</a>\n\nУчастники: X, Y\n\nBody1.\nBody2.\n"
    out = _strip_prior_short_summary_body(s)
    assert "Участники" not in out
    assert "Body1." in out
    assert "Body2." in out


def test_fr_cr_05_192u_strip_removes_trailing_todo_block() -> None:
    """The «TODO:» trailer + numbered tasks MUST not bleed into
    the agenda recap (those are already in the thread reply)."""
    s = (
        '<a href="u">T</a>\n\n'
        "Участники: A\n\n"
        "Recap body.\n\n"
        "TODO:\n"
        "1) Old task — Owner • 22.05.2026 18:00\n"
        "2) Another — Owner • 22.05.2026 18:00\n"
    )
    out = _strip_prior_short_summary_body(s)
    assert out == "Recap body."


def test_fr_cr_05_192u_strip_empty_or_missing() -> None:
    assert _strip_prior_short_summary_body("") == ""
    assert _strip_prior_short_summary_body(None) == ""  # type: ignore[arg-type]


def test_fr_cr_05_192u_strip_decodes_html_entities() -> None:
    """The stored short_summary has `&lt;` `&gt;` `&amp;` in body
    (FR-CR-05-127 html.escape pass). Recap MUST be decoded plain
    text for the operator to read."""
    s = (
        '<a href="u">T</a>\n\n'
        "Участники: A\n\n"
        "Обсуждали X &lt;&gt; Y и &amp; Z.\n"
    )
    out = _strip_prior_short_summary_body(s)
    assert "<>" in out
    assert "&" in out
    assert "&amp;" not in out


# --------------------------------------------------------------------------- #
# _compose_lite — full output shape, no LLM
# --------------------------------------------------------------------------- #


def test_fr_cr_05_192u_compose_lite_no_llm_call() -> None:
    """`_compose_lite` MUST NOT touch llm_backend at all. The
    function signature doesn't even take one — caller-proof
    against accidental wiring."""
    c = _candidate(
        prior=[{
            "short_summary": (
                '<a href="u">21/05 - Fundraising daily</a>\n\n'
                "Участники: A, B\n\n"
                "Обсудили статус раунда.\n"
            ),
            "google_doc_url": "https://docs.google.com/x",
            "meeting_date": "2026-05-21T11:30:00+00:00",
        }],
        open_tasks=[{"title": "Push intro", "owner": "Ирина"}],
    )
    out = _compose_lite(c)
    assert out is not None
    assert out.previous_recap == ["Обсудили статус раунда."]


def test_fr_cr_05_192u_compose_lite_open_questions_always_empty() -> None:
    """Operator pinned: agenda doesn't manufacture discussion items."""
    c = _candidate(
        prior=[{"short_summary": (
            '<a href="u">T</a>\n\nУчастники: A\n\nBody.\n'
        )}],
        open_tasks=[],
    )
    out = _compose_lite(c)
    assert out is not None
    assert out.open_questions == []


def test_fr_cr_05_192u_compose_lite_tasks_passthrough() -> None:
    """Tasks MUST be reproduced verbatim — title, description,
    owner, due, status pass through unchanged."""
    c = _candidate(
        prior=[{"short_summary": (
            '<a href="u">T</a>\n\nУчастники: A\n\nBody.\n'
        )}],
        open_tasks=[
            {
                "task_id": 42,
                "title": "Send deck",
                "description": "non-NDA",
                "owner": "Артем Соколов",
                "due": "22.05.2026",
                "status": "todo",
            },
        ],
    )
    out = _compose_lite(c)
    assert out is not None
    assert len(out.tasks_checklist) == 1
    t = out.tasks_checklist[0]
    assert t["task_id"] == 42
    assert t["title"] == "Send deck"
    assert t["description"] == "non-NDA"
    assert t["owner"] == "Артем Соколов"
    assert t["due"] == "22.05.2026"
    assert t["status"] == "todo"


def test_fr_cr_05_192u_compose_lite_empty_priors() -> None:
    """When no prior recording exists, recap is empty list. open
    tasks still pass through. doc_body_md still built (with
    «(нет данных)» placeholder)."""
    c = _candidate(prior=[], open_tasks=[{"title": "T"}])
    out = _compose_lite(c)
    assert out is not None
    assert out.previous_recap == []
    # doc_body_md must NOT duplicate the wrapper's «# Повестка…» H1
    # — wrapper prepends that and the «## Подробно по прошлой
    # встрече» block. The body starts at «## На прошлой встрече».
    assert out.doc_body_md.startswith("## На прошлой встрече")
    assert "# Повестка" not in out.doc_body_md
    assert "## Подробно по прошлой встрече" not in out.doc_body_md
    assert "(нет данных по прошлой встрече)" in out.doc_body_md


def test_fr_cr_05_192u_compose_lite_doc_body_has_recap_and_tasks_table() -> None:
    """`doc_body_md` MUST carry both `## На прошлой встрече` +
    recap text AND `## К обсуждению` markdown table with every
    open task as a row."""
    c = _candidate(
        prior=[{
            "short_summary": (
                '<a href="u">21/05 - T</a>\n\n'
                "Участники: A\n\nRecap-line.\n"
            ),
            "google_doc_url": "https://docs/x",
            "meeting_date": "2026-05-21T11:30:00+00:00",
        }],
        open_tasks=[
            {"title": "Task A", "owner": "X"},
            {"title": "Task B", "owner": "Y"},
        ],
    )
    out = _compose_lite(c)
    assert out is not None
    md = out.doc_body_md
    assert "## На прошлой встрече" in md
    assert "Recap-line." in md
    assert "## К обсуждению" in md
    # Each task is a row
    assert "| 1 | Task A" in md
    assert "| 2 | Task B" in md


# --------------------------------------------------------------------------- #
# compose_agenda — public entrypoint MUST route to lite, NOT LLM
# --------------------------------------------------------------------------- #


def test_fr_cr_05_192u_public_compose_agenda_ignores_llm_backend() -> None:
    """Public `compose_agenda(candidate, llm_backend=..., model=...)`
    MUST NOT call llm_backend.complete_json / chat.completions
    even when passed a real-looking backend. Operator-pinned
    «никакого LLM»."""
    fake_llm = MagicMock()
    fake_llm.complete_json = MagicMock(
        side_effect=AssertionError("LLM call leaked into compose_agenda")
    )
    fake_llm._client = MagicMock()
    fake_llm._client.chat.completions.create = MagicMock(
        side_effect=AssertionError("OpenAI client call leaked into compose_agenda")
    )
    c = _candidate(
        prior=[{"short_summary": (
            '<a href="u">T</a>\n\nУчастники: A\n\nBody-recap.\n'
        )}],
        open_tasks=[{"title": "x"}],
    )
    out = compose_agenda(c, llm_backend=fake_llm, model="gpt-5.5")
    assert out is not None
    assert out.previous_recap == ["Body-recap."]
    fake_llm.complete_json.assert_not_called()
    fake_llm._client.chat.completions.create.assert_not_called()


__all__ = []  # type: ignore[var-annotated]
