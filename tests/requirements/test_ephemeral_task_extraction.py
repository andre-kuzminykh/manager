"""FR-CR-05-192k — ID-locked tests for the ephemeral task-extraction
path used by `send_one_fireflies` + `send_one_zoom`.

Production regression on 2026-05-22 (#1 Object First): the ephemeral
path posted 33 tasks to Slack with raw emails as owner_display_name
(`sots@thehumanoid.ai`, `jochen@thehumanoid.ai`, `jarc@thehumanoid.ai`)
instead of canonical real names. Operator-pinned response:

  «ты должен ходить в таблицу и имена подсовывать в зависимости
   от Notes и должности»
  «надо с задачами туда но не в бд»
  «и они отфильтрованы должны быть, с ответственным из таблицы и сроком»

The contract these tests lock:

  PROMPT-SIDE
    - user_prompt MUST inline `_render_known_employees_table(...)`
      so the LLM picks owner from the directory (slack_user_id +
      real_name + role + notes columns).
    - The JSON-shape line MUST forbid raw emails as owner.

  RESOLVER-SIDE
    - 3-stage lookup with prefetched TM maps:
        1. slack_user_id (U-prefixed) → real_name
        2. email (contains @) → real_name
        3. exact real_name membership → passthrough
    - Unknown owner: returned unchanged (so operator sees the
      regression on the next trace run instead of silently
      eating data).

  RENDER-SIDE
    - `_render_tasks_block` uses task['due_date'] + task['due_time']
      when the LLM emitted them (FR-CR-05-185 alignment).
    - Falls back to «today 18:00» only when both are absent.
    - Uses description (if non-empty) else title for body text.
    - Caps body at 350 chars to mirror `_build_todo_section`.

Anything below that contradicts this contract is the regression
itself and the test fails.
"""
from __future__ import annotations

from datetime import date, time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.models import TeamMember
from ops.send_summaries_19_21 import (
    _extract_important_tasks_ephemeral,
    _render_tasks_block,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _seed_tm(session) -> None:
    """Three TM rows mirroring the production directory shape on
    2026-05-22 after `ops/fix_team_member_emails.py` ran."""
    session.add_all([
        TeamMember(
            real_name="Sotirios Stasinopoulos",
            email="sots@thehumanoid.ai",
            slack_user_id="U080QBXNLJJ",
            role="Chief Product Officer",
            active=True,
        ),
        TeamMember(
            real_name="Jochen Ruda",
            email="jochen@thehumanoid.ai",
            slack_user_id="U0937HYSB7Z",
            role="CRO / CGO",
            active=True,
        ),
        TeamMember(
            real_name="Jarad Cannon",
            email="jarc@thehumanoid.ai",
            slack_user_id="U08BF50S09W",
            role="CTO",
            active=True,
        ),
    ])
    session.flush()


def _make_row(meeting_date=None):
    """Light stub of `MeetingRecording` carrying just the columns
    `_extract_important_tasks_ephemeral` reads."""
    from datetime import datetime, timezone
    return SimpleNamespace(
        title="Test meeting",
        meeting_date=meeting_date or datetime(2026, 5, 19, 13, 0, tzinfo=timezone.utc),
        transcript_text="x " * 1000,
        detailed_summary="y " * 200,
        calendar_attendees=[],
        participants=[],
    )


class _CapturingLLM:
    """LLM stub that captures the user_prompt for assertion and
    returns a pre-baked JSON response."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.last_user_prompt: str | None = None
        self.last_system_prompt: str | None = None

    def complete_text(
        self, *, system_prompt: str, user_prompt: str, **kwargs,
    ) -> str:
        self.last_system_prompt = system_prompt
        self.last_user_prompt = user_prompt
        return self.response


# --------------------------------------------------------------------------- #
# PROMPT-SIDE tests — known_employees table + email forbidden
# --------------------------------------------------------------------------- #


def test_fr_cr_05_192k_prompt_includes_known_employees_table(session, patched_session_scope) -> None:
    """user_prompt must inline the directory table the live pipeline
    passes to TASK_EXTRACTION_SYSTEM — slack_user_id + display_name +
    real_name + role + notes columns."""
    _seed_tm(session)
    llm = _CapturingLLM(response='{"tasks": []}')
    with patch(
        "ops.send_summaries_19_21.classify_directions",
        return_value={},
    ):
        _extract_important_tasks_ephemeral(
            _make_row(),
            settings=SimpleNamespace(
                fireflies_tasks_model="gpt-5.5",
                fireflies_tasks_reasoning_effort="high",
            ),
            llm_backend=llm,
        )
    up = llm.last_user_prompt or ""
    assert "known_employees" in up
    # Column header from _render_known_employees_table
    assert "slack_user_id" in up
    assert "real_name" in up
    assert "role" in up
    # Each seeded row must appear by canonical real_name
    assert "Sotirios Stasinopoulos" in up
    assert "Jochen Ruda" in up
    assert "Jarad Cannon" in up


def test_fr_cr_05_192k_prompt_forbids_raw_email_as_owner(session, patched_session_scope) -> None:
    """The JSON-shape line in user_prompt MUST explicitly forbid the
    LLM from emitting raw emails as owner — this is the contract
    that prevents the 2026-05-22 regression."""
    _seed_tm(session)
    llm = _CapturingLLM(response='{"tasks": []}')
    with patch(
        "ops.send_summaries_19_21.classify_directions",
        return_value={},
    ):
        _extract_important_tasks_ephemeral(
            _make_row(),
            settings=SimpleNamespace(
                fireflies_tasks_model="gpt-5.5",
                fireflies_tasks_reasoning_effort="high",
            ),
            llm_backend=llm,
        )
    up = (llm.last_user_prompt or "").lower()
    # Either «never» or «forbidden» language + «email» mention
    # within the JSON-shape paragraph (top of the prompt).
    assert "never" in up and "email" in up


# --------------------------------------------------------------------------- #
# RESOLVER-SIDE tests — 3-stage lookup
# --------------------------------------------------------------------------- #


def _llm_with_owners(owners: list[str]) -> _CapturingLLM:
    import json as _json
    tasks = [
        {
            "title": f"task #{i}",
            "description": f"desc {i}",
            "owner": o,
            "priority": "medium",
        }
        for i, o in enumerate(owners, start=1)
    ]
    return _CapturingLLM(_json.dumps({"tasks": tasks}))


def test_fr_cr_05_192k_resolver_maps_email_to_real_name(session, patched_session_scope) -> None:
    """LLM regressed and emitted an email — resolver MUST swap it
    for the TeamMember.real_name keyed by email (case-insensitive)."""
    _seed_tm(session)
    llm = _llm_with_owners(["sots@thehumanoid.ai"])
    with patch(
        "ops.send_summaries_19_21.classify_directions",
        return_value={0: "investors"},  # important → not filtered
    ):
        out = _extract_important_tasks_ephemeral(
            _make_row(),
            settings=SimpleNamespace(
                fireflies_tasks_model="gpt-5.5",
                fireflies_tasks_reasoning_effort=None,
            ),
            llm_backend=llm,
        )
    assert len(out) == 1
    assert out[0]["owner"] == "Sotirios Stasinopoulos"


def test_fr_cr_05_192k_resolver_maps_slack_user_id_to_real_name(session, patched_session_scope) -> None:
    """LLM did the right thing — picked a slack_user_id from the
    table. Resolver MUST surface the canonical real_name."""
    _seed_tm(session)
    llm = _llm_with_owners(["U0937HYSB7Z"])  # Jochen
    with patch(
        "ops.send_summaries_19_21.classify_directions",
        return_value={0: "investors"},
    ):
        out = _extract_important_tasks_ephemeral(
            _make_row(),
            settings=SimpleNamespace(
                fireflies_tasks_model="gpt-5.5",
                fireflies_tasks_reasoning_effort=None,
            ),
            llm_backend=llm,
        )
    assert len(out) == 1
    assert out[0]["owner"] == "Jochen Ruda"


def test_fr_cr_05_192k_resolver_passes_through_canonical_real_name(session, patched_session_scope) -> None:
    """LLM emitted the canonical real_name directly — resolver MUST
    leave it unchanged (idempotent)."""
    _seed_tm(session)
    llm = _llm_with_owners(["Jarad Cannon"])
    with patch(
        "ops.send_summaries_19_21.classify_directions",
        return_value={0: "investors"},
    ):
        out = _extract_important_tasks_ephemeral(
            _make_row(),
            settings=SimpleNamespace(
                fireflies_tasks_model="gpt-5.5",
                fireflies_tasks_reasoning_effort=None,
            ),
            llm_backend=llm,
        )
    assert len(out) == 1
    assert out[0]["owner"] == "Jarad Cannon"


def test_fr_cr_05_192k_resolver_passes_through_unknown_owner(session, patched_session_scope) -> None:
    """Owner not in the directory (external attendee / hallucination)
    — resolver MUST return it unchanged so the operator catches it
    on the next trace run instead of silently swallowing the value."""
    _seed_tm(session)
    llm = _llm_with_owners(["External Person"])
    with patch(
        "ops.send_summaries_19_21.classify_directions",
        return_value={0: "investors"},
    ):
        out = _extract_important_tasks_ephemeral(
            _make_row(),
            settings=SimpleNamespace(
                fireflies_tasks_model="gpt-5.5",
                fireflies_tasks_reasoning_effort=None,
            ),
            llm_backend=llm,
        )
    assert len(out) == 1
    assert out[0]["owner"] == "External Person"


# --------------------------------------------------------------------------- #
# RENDER-SIDE tests — _render_tasks_block
# --------------------------------------------------------------------------- #


def test_fr_cr_05_192k_render_uses_llm_due_date_when_present() -> None:
    """When the LLM emitted `due_date`+`due_time`, the rendered line
    MUST carry those values — NOT the «today 18:00» fallback."""
    out = _render_tasks_block([{
        "title": "T",
        "description": "Поднять commit Bosch до конца месяца",
        "owner": "Jarad Cannon",
        "due_date": "2026-05-31",
        "due_time": "10:30",
    }])
    assert "31.05.2026 10:30" in out
    assert "Jarad Cannon" in out


def test_fr_cr_05_192k_render_falls_back_to_today_18_when_no_due_date() -> None:
    """When the LLM omits due_date, the renderer falls back to today
    at 18:00 (FR-CR-05-119 convention). due_time alone is ignored —
    a deadline without a date is meaningless."""
    today_18 = f"{date.today().strftime('%d.%m.%Y')} 18:00"
    out = _render_tasks_block([{
        "title": "T",
        "description": "D",
        "owner": "Jochen Ruda",
        "due_date": None,
        "due_time": None,
    }])
    assert today_18 in out


def test_fr_cr_05_192k_render_uses_description_then_title() -> None:
    """Body text MUST prefer description when non-empty, fall back
    to title when description is blank (mirrors _build_todo_section)."""
    out_desc = _render_tasks_block([{
        "title": "short",
        "description": "rich description text",
        "owner": "A",
        "due_date": None, "due_time": None,
    }])
    assert "rich description text" in out_desc
    assert "1) rich description text" in out_desc

    out_title = _render_tasks_block([{
        "title": "title-only",
        "description": "",
        "owner": "A",
        "due_date": None, "due_time": None,
    }])
    assert "title-only" in out_title


def test_fr_cr_05_192k_render_caps_long_description_at_350() -> None:
    """Bodies > 350 chars MUST be soft-capped at the last space ≤350
    and trail with «…». Mirrors `_build_todo_section`'s
    `len > 350 → rfind(' ', 0, 350)` slicer."""
    long_desc = "слово " * 200  # 1200 chars
    out = _render_tasks_block([{
        "title": "T",
        "description": long_desc,
        "owner": "A",
        "due_date": None, "due_time": None,
    }])
    # The body up to the « — Owner • DD.MM.YYYY HH:MM» suffix.
    body_line = out.split(" — ", 1)[0]
    # «1) » prefix + body. The cap is on body text, not prefix.
    body_text = body_line[len("1) "):]
    assert body_text.endswith("…")
    assert len(body_text) <= 351  # 350 chars + «…» (1 char)


# --------------------------------------------------------------------------- #
# FR-CR-05-192r — delegate via TM notes
# --------------------------------------------------------------------------- #


def _seed_tm_with_delegate(session) -> None:
    """Seed Артем + Ирина with the delegate marker on Артем's notes."""
    session.add_all([
        TeamMember(
            real_name="Артем Соколов",
            email="1@thehumanoid.ai",
            active=True,
            notes=(
                "DELEGATE_TASKS_TO: Ирина Шипилова. "
                "Operator-pinned 2026-05-22: CEO does not own "
                "actionable items."
            ),
        ),
        TeamMember(
            real_name="Ирина Шипилова",
            active=True,
            notes="Handles CEO follow-ups.",
        ),
    ])
    session.flush()


def test_fr_cr_05_192r_resolver_swaps_delegate_when_notes_marker_present(
    session, patched_session_scope,
) -> None:
    """When the LLM picks Артем as task owner and Артем's TM notes
    carry DELEGATE_TASKS_TO: Ирина Шипилова, the resolver MUST
    substitute Ирина."""
    _seed_tm_with_delegate(session)
    llm = _llm_with_owners(["Артем Соколов"])
    with patch(
        "ops.send_summaries_19_21.classify_directions",
        return_value={0: "investors"},
    ):
        out = _extract_important_tasks_ephemeral(
            _make_row(),
            settings=SimpleNamespace(
                fireflies_tasks_model="gpt-5.5",
                fireflies_tasks_reasoning_effort=None,
            ),
            llm_backend=llm,
        )
    assert len(out) == 1
    assert out[0]["owner"] == "Ирина Шипилова"


def test_fr_cr_05_192r_resolver_skips_delegate_when_target_not_in_tm(
    session, patched_session_scope,
) -> None:
    """Sanity guard: if the delegate target isn't a known TM
    real_name, the resolver keeps the original owner (no silent
    typo-eating). Operator pinned the «never typo into the void»
    rule as part of FR-CR-05-192k."""
    session.add(
        TeamMember(
            real_name="Артем Соколов",
            email="1@thehumanoid.ai",
            active=True,
            notes="DELEGATE_TASKS_TO: Nonexistent Person",
        )
    )
    session.flush()
    llm = _llm_with_owners(["Артем Соколов"])
    with patch(
        "ops.send_summaries_19_21.classify_directions",
        return_value={0: "investors"},
    ):
        out = _extract_important_tasks_ephemeral(
            _make_row(),
            settings=SimpleNamespace(
                fireflies_tasks_model="gpt-5.5",
                fireflies_tasks_reasoning_effort=None,
            ),
            llm_backend=llm,
        )
    assert len(out) == 1
    # Delegate target missing → keep canonical Артем
    assert out[0]["owner"] == "Артем Соколов"


def test_fr_cr_05_192r_resolver_no_delegate_when_marker_absent(
    session, patched_session_scope,
) -> None:
    """When the owner has no DELEGATE marker in notes, the resolver
    leaves the canonical real_name in place (no surprise swaps)."""
    session.add_all([
        TeamMember(
            real_name="Дмитрий Седов",
            email="dmitry.sedov@thehumanoid.ai",
            active=True,
            notes="CFO. No delegate.",
        ),
        TeamMember(
            real_name="Ирина Шипилова",
            active=True,
        ),
    ])
    session.flush()
    llm = _llm_with_owners(["Дмитрий Седов"])
    with patch(
        "ops.send_summaries_19_21.classify_directions",
        return_value={0: "budget"},
    ):
        out = _extract_important_tasks_ephemeral(
            _make_row(),
            settings=SimpleNamespace(
                fireflies_tasks_model="gpt-5.5",
                fireflies_tasks_reasoning_effort=None,
            ),
            llm_backend=llm,
        )
    assert len(out) == 1
    assert out[0]["owner"] == "Дмитрий Седов"


def test_fr_cr_05_192r_delegate_marker_case_insensitive(
    session, patched_session_scope,
) -> None:
    """Marker parsing MUST be case-insensitive so operator can type
    `delegate_tasks_to:` or `DELEGATE_TASKS_TO:` interchangeably in
    the Team sheet."""
    session.add_all([
        TeamMember(
            real_name="Артем Соколов",
            email="1@thehumanoid.ai",
            active=True,
            notes="delegate_tasks_to: Ирина Шипилова",  # lowercase
        ),
        TeamMember(
            real_name="Ирина Шипилова",
            active=True,
        ),
    ])
    session.flush()
    llm = _llm_with_owners(["Артем Соколов"])
    with patch(
        "ops.send_summaries_19_21.classify_directions",
        return_value={0: "investors"},
    ):
        out = _extract_important_tasks_ephemeral(
            _make_row(),
            settings=SimpleNamespace(
                fireflies_tasks_model="gpt-5.5",
                fireflies_tasks_reasoning_effort=None,
            ),
            llm_backend=llm,
        )
    assert len(out) == 1
    assert out[0]["owner"] == "Ирина Шипилова"


__all__ = []  # type: ignore[var-annotated]
