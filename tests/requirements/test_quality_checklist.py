"""FR-CR-05-192q — ID-locked tests for the 20-point quality
checklist the operator pinned on 2026-05-22.

Each test fixes one check from `ops/quality_checklist._make_check_table`
in place so future refactors can't silently drop a check or flip its
semantics. Status semantics:
  ✓ — automatic verification passed
  ✗ — automatic verification failed
  ~ — partial / manual review required
  — — check not applicable (e.g. «task owner» when there are no tasks)

The tests build a minimal `ZoomRecording` / `MeetingRecording` fixture
in-memory, exercise `_make_check_table` directly with the session
fixture, and assert the targeted check's status + a substring of the
trace comment.
"""
from __future__ import annotations

from datetime import datetime, time, timezone

from app.models import (
    MeetingRecording,
    TeamMember,
    Task,
    TaskPriority,
    TaskSourceKind,
    TaskStatus,
    ZoomRecording,
)
from app.models.counterparty import Counterparty
from ops.quality_checklist import _make_check_table


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _row(**overrides):
    """Build a Zoom row stub with defaults adequate for most checks."""
    defaults = dict(
        zoom_id="zz==",
        title="Zoom Meeting",
        meeting_date=datetime(2026, 5, 20, 9, 56, tzinfo=timezone.utc),
        duration_seconds=3600,
        google_doc_url="https://docs.google.com/document/d/test/edit",
        transcript_text="hello world " * 200,
        detailed_summary=(
            "📅 МЕТА\n\nДата и продолжительность: 20.05.2026.\n\n"
            "Участники: Alice, Bob, …\n\n"
        ) + ("body text " * 200),
        short_summary=(
            '<a href="https://docs.google.com/document/d/test/edit">'
            '20/05 - Test</a>\n\n'
            "Участники: Alice, Bob\n\n"
            "Это короткое саммари.\n"
        ),
        calendar_attendees=[],
        participants=[],
    )
    defaults.update(overrides)
    return ZoomRecording(**defaults)


def _get_check(rows, n: int) -> tuple[int, str, str, str]:
    for r in rows:
        if r[0] == n:
            return r
    raise AssertionError(f"check {n} not found")


def _checks(session, row):
    return _make_check_table(
        row,
        "zoom" if isinstance(row, ZoomRecording) else "fireflies",
        members_by_norm={},
        cp_norms=[],
        session=session,
    )


# --------------------------------------------------------------------------- #
# Block 1 — record metadata (checks 1-5)
# --------------------------------------------------------------------------- #


def test_fr_cr_05_192q_check_01_source_stamped(session) -> None:
    r = _row()
    session.add(r); session.flush()
    n, name, status, comment = _get_check(_checks(session, r), 1)
    assert name == "Источник"
    assert status == "✓"
    assert "zoom" in comment.lower()


def test_fr_cr_05_192q_check_02_title_non_empty(session) -> None:
    r_ok = _row(zoom_id="zz1==", title="Real Title")
    r_bad = _row(zoom_id="zz2==", title="")
    session.add_all([r_ok, r_bad]); session.flush()
    assert _get_check(_checks(session, r_ok), 2)[2] == "✓"
    assert _get_check(_checks(session, r_bad), 2)[2] == "✗"


def test_fr_cr_05_192q_check_03_google_doc_url_required(session) -> None:
    r_ok = _row(zoom_id="zz3==", google_doc_url="https://docs.google.com/x")
    r_bad = _row(zoom_id="zz4==", google_doc_url="")
    session.add_all([r_ok, r_bad]); session.flush()
    assert _get_check(_checks(session, r_ok), 3)[2] == "✓"
    assert _get_check(_checks(session, r_bad), 3)[2] == "✗"


def test_fr_cr_05_192q_check_04_detailed_summary_has_mandatory_fields(
    session,
) -> None:
    r_ok = _row(
        zoom_id="zz5==",
        detailed_summary=(
            "📅 МЕТА\n\nУчастники: A, B\n\n" + ("body " * 250)
        ),
    )
    r_missing_meta = _row(
        zoom_id="zz6==",
        detailed_summary="Участники: A\n\n" + ("body " * 250),
    )
    r_missing_uchastniki = _row(
        zoom_id="zz7==",
        detailed_summary="📅 МЕТА\n\nNo audience.\n" + ("body " * 250),
    )
    session.add_all([r_ok, r_missing_meta, r_missing_uchastniki])
    session.flush()
    assert _get_check(_checks(session, r_ok), 4)[2] == "✓"
    # Missing meta or участники → partial (~), not strict fail
    assert _get_check(_checks(session, r_missing_meta), 4)[2] == "~"
    assert _get_check(_checks(session, r_missing_uchastniki), 4)[2] == "~"


def test_fr_cr_05_192q_check_05_detailed_summary_depth_threshold(
    session,
) -> None:
    """≥5000 chars = ✓; 1500-4999 = ~; <1500 = ✗."""
    r_deep = _row(zoom_id="zz8==", detailed_summary="x" * 5500)
    r_mid = _row(zoom_id="zz9==", detailed_summary="x" * 2500)
    r_shallow = _row(zoom_id="zz10==", detailed_summary="x" * 800)
    session.add_all([r_deep, r_mid, r_shallow]); session.flush()
    assert _get_check(_checks(session, r_deep), 5)[2] == "✓"
    assert _get_check(_checks(session, r_mid), 5)[2] == "~"
    assert _get_check(_checks(session, r_shallow), 5)[2] == "✗"


# --------------------------------------------------------------------------- #
# Block 2 — transcript + canonicalization (checks 6-7)
# --------------------------------------------------------------------------- #


def test_fr_cr_05_192q_check_06_transcript_bilingual_3percent_each(
    session,
) -> None:
    """Bilingual = ≥3% Cyrillic AND ≥3% Latin in transcript."""
    bilingual_text = ("Hello мир, " * 200)
    monolingual_lat = ("hello world " * 200)
    r_bi = _row(zoom_id="zz11==", transcript_text=bilingual_text)
    r_mono = _row(zoom_id="zz12==", transcript_text=monolingual_lat)
    session.add_all([r_bi, r_mono]); session.flush()
    assert _get_check(_checks(session, r_bi), 6)[2] == "✓"
    assert _get_check(_checks(session, r_mono), 6)[2] == "✗"


def test_fr_cr_05_192q_check_07_email_leaks_flagged_in_detailed(
    session,
) -> None:
    """An `@thehumanoid.ai` email in detailed_summary flags partial-pass
    on canonicalization — operator sees raw emails in the trace."""
    r_clean = _row(
        zoom_id="zz13==",
        detailed_summary=(
            "📅 МЕТА\n\nУчастники: Alice\n\n"
            + ("Aramco discussion. " * 100)
        ),
    )
    r_leak = _row(
        zoom_id="zz14==",
        detailed_summary=(
            "📅 МЕТА\n\nУчастники: sots@thehumanoid.ai, jarc@thehumanoid.ai\n\n"
            + ("body text " * 100)
        ),
    )
    session.add_all([r_clean, r_leak])
    # Need at least one Counterparty match or TM match for the «✓» branch
    session.add(Counterparty(name="Aramco", name_normalised="aramco"))
    session.flush()
    leak_check = _get_check(_checks(session, r_leak), 7)
    assert leak_check[2] == "~"
    assert "email-leaks=2" in leak_check[3]
    assert "sots@thehumanoid.ai" in leak_check[3]


# --------------------------------------------------------------------------- #
# Block 3 — title link (check 8)
# --------------------------------------------------------------------------- #


def test_fr_cr_05_192q_check_08_title_first_line_has_hyperlink(
    session,
) -> None:
    r_ok = _row(zoom_id="zz15==")  # default short_summary starts with <a href>
    r_no_link = _row(
        zoom_id="zz16==",
        short_summary="20/05 - Plain title\n\nУчастники: …\n\nbody",
    )
    session.add_all([r_ok, r_no_link]); session.flush()
    assert _get_check(_checks(session, r_ok), 8)[2] == "✓"
    assert _get_check(_checks(session, r_no_link), 8)[2] == "✗"


# --------------------------------------------------------------------------- #
# Block 4 — participants (checks 9-11)
# --------------------------------------------------------------------------- #


def test_fr_cr_05_192q_check_09_participants_vs_calendar(session) -> None:
    r_with_cal = _row(
        zoom_id="zz17==",
        calendar_attendees=[
            {"email": "a@x.com", "resolved_name": "Alice",
             "source": "team_member"},
            {"email": "b@x.com", "resolved_name": "Bob",
             "source": "team_member"},
        ],
    )
    r_no_cal = _row(zoom_id="zz18==", calendar_attendees=[])
    session.add_all([r_with_cal, r_no_cal]); session.flush()
    ok = _get_check(_checks(session, r_with_cal), 9)
    assert ok[2] == "✓"
    assert "calendar_attendees=2" in ok[3]
    assert "resolved_to_TM=2" in ok[3]
    assert _get_check(_checks(session, r_no_cal), 9)[2] == "✗"


def test_fr_cr_05_192q_check_10_participants_vs_raw_zoom_fireflies(
    session,
) -> None:
    r_with_raw = _row(
        zoom_id="zz19==",
        participants=["Alice", "Bob"],
    )
    r_no_raw = _row(zoom_id="zz20==", participants=[])
    session.add_all([r_with_raw, r_no_raw]); session.flush()
    assert _get_check(_checks(session, r_with_raw), 10)[2] == "✓"
    # No raw participants — partial (~), not strict fail (zoom often
    # has no host-side roster).
    assert _get_check(_checks(session, r_no_raw), 10)[2] == "~"


def test_fr_cr_05_192q_check_11_full_list_from_all_sources(session) -> None:
    """short summary «Участники:» line overlaps with calendar_attendees
    on ≥1 name; «и другие» required when calendar set is larger."""
    r_good = _row(
        zoom_id="zz21==",
        calendar_attendees=[
            {"email": "a@x.com", "resolved_name": "Alice",
             "source": "team_member"},
            {"email": "b@x.com", "resolved_name": "Bob",
             "source": "team_member"},
        ],
        short_summary=(
            '<a href="https://docs.google.com/document/d/test/edit">'
            '20/05 - Test</a>\n\n'
            'Участники: Alice, Bob\n\n'
            'body\n'
        ),
    )
    session.add(r_good); session.flush()
    assert _get_check(_checks(session, r_good), 11)[2] == "✓"


# --------------------------------------------------------------------------- #
# Block 5 — short summary (check 12)
# --------------------------------------------------------------------------- #


def test_fr_cr_05_192q_check_12_short_summary_present(session) -> None:
    r_ok = _row(zoom_id="zz22==")
    r_empty = _row(zoom_id="zz23==", short_summary="")
    session.add_all([r_ok, r_empty]); session.flush()
    assert _get_check(_checks(session, r_ok), 12)[2] == "✓"
    assert _get_check(_checks(session, r_empty), 12)[2] == "✗"


# --------------------------------------------------------------------------- #
# Block 6 — To-Do (checks 13-18)
# --------------------------------------------------------------------------- #


def _add_task(session, conv_id: str, *, owner: str = "Alice",
              direction: str | None = "investors",
              due_date=None, title: str = "Send follow-up") -> Task:
    t = Task(
        title=title,
        description=title,
        owner_display_name=owner,
        priority=TaskPriority.medium,
        status=TaskStatus.backlog,
        due_date=due_date,
        source_kind=TaskSourceKind.zoom,
        source_conversation_id=conv_id,
        extra={"direction": direction} if direction else {},
    )
    session.add(t); session.flush()
    return t


def test_fr_cr_05_192q_check_13_tasks_present_when_actionable_in_transcript(
    session,
) -> None:
    """When transcript carries actionable signals but DB has 0 tasks → ~."""
    r = _row(
        zoom_id="zz24==",
        transcript_text=("надо подготовить отчёт " * 300),
    )
    session.add(r); session.flush()
    assert _get_check(_checks(session, r), 13)[2] == "~"


def test_fr_cr_05_192q_check_14_tasks_filtered_by_direction_important(
    session,
) -> None:
    """`extra.direction in DIRECTIONS_IMPORTANT` keeps task; other drops it."""
    r = _row(zoom_id="zz25==")
    session.add(r); session.flush()
    _add_task(session, "zz25==", direction="investors", title="A")
    _add_task(session, "zz25==", direction="other", title="B")
    check = _get_check(_checks(session, r), 14)
    assert check[2] == "✓"
    assert "important=1/2" in check[3]


def test_fr_cr_05_192q_check_15_task_owners_in_team_member(session) -> None:
    """When all task owners exist in TM → ✓; email-form or unknown → ✗."""
    session.add(
        TeamMember(real_name="Alice", email="alice@x.com", active=True)
    )
    r = _row(zoom_id="zz26==")
    session.add(r); session.flush()
    _add_task(session, "zz26==", owner="Alice")
    # Re-build members_by_norm for this test (override default)
    members_by_norm = {"alice": session.query(TeamMember).first()}
    rows = _make_check_table(
        r, "zoom",
        members_by_norm=members_by_norm,
        cp_norms=[],
        session=session,
    )
    assert _get_check(rows, 15)[2] == "✓"
    # Now flip to email-form owner
    _add_task(session, "zz26==", owner="alien@x.com")
    rows = _make_check_table(
        r, "zoom",
        members_by_norm=members_by_norm, cp_norms=[], session=session,
    )
    assert _get_check(rows, 15)[2] == "✗"


def test_fr_cr_05_192q_check_16_tasks_anchored_to_detailed_summary(
    session,
) -> None:
    """A task whose title/description shares ≥1 multi-char word with
    detailed_summary is «anchored». 100% anchored → ✓, else → ~."""
    r = _row(
        zoom_id="zz27==",
        detailed_summary=(
            "📅 МЕТА\n\nУчастники: Alice\n\n"
            + ("обсудили запуск deployment с клиентом " * 50)
        ),
    )
    session.add(r); session.flush()
    _add_task(session, "zz27==", title="запустить deployment у клиента")
    assert _get_check(_checks(session, r), 16)[2] == "✓"
    # Add an unrelated task
    _add_task(session, "zz27==", title="zzz xxx yyy 1234")
    assert _get_check(_checks(session, r), 16)[2] == "~"


def test_fr_cr_05_192q_check_17_task_deadlines_real_or_default(
    session,
) -> None:
    """At least one task with `due_date` ≠ default meeting_date 18:00 → ✓."""
    from datetime import date as _date
    r = _row(zoom_id="zz28==")
    session.add(r); session.flush()
    # All defaults (meeting_date)
    _add_task(session, "zz28==", due_date=r.meeting_date.date())
    assert _get_check(_checks(session, r), 17)[2] == "~"
    # Add a task with a non-default deadline
    _add_task(session, "zz28==", due_date=_date(2026, 6, 1),
              title="next month task")
    assert _get_check(_checks(session, r), 17)[2] == "✓"


def test_fr_cr_05_192q_check_18_missing_deadlines_flagged_when_relative_markers(
    session,
) -> None:
    """If all task deadlines are default AND transcript carries relative
    markers («завтра», «к понедельник»…) → ~ (LLM may have missed)."""
    r = _row(
        zoom_id="zz29==",
        transcript_text=("сделать это завтра в 18:00 " * 100),
    )
    session.add(r); session.flush()
    _add_task(session, "zz29==", due_date=r.meeting_date.date())
    check = _get_check(_checks(session, r), 18)
    assert check[2] == "~"
    assert "relative-маркеры" in check[3] or "default-дедлайном" in check[3]


# --------------------------------------------------------------------------- #
# Block 7 — final checks (19-20)
# --------------------------------------------------------------------------- #


def test_fr_cr_05_192q_check_19_no_hallucinations_email_leak_or_unverified(
    session,
) -> None:
    """Email-leak in detailed OR participant in short-line not in TM /
    calendar → flagged as ~ for manual review."""
    r_leak = _row(
        zoom_id="zz30==",
        detailed_summary=(
            "📅 МЕТА\n\nУчастники: …\n\n"
            + "Mentions sots@thehumanoid.ai in body. " * 50
        ),
    )
    session.add(r_leak); session.flush()
    check = _get_check(_checks(session, r_leak), 19)
    assert check[2] == "~"
    assert "email-leak" in check[3]


def test_fr_cr_05_192q_check_20_uncertain_markers_default_to_manual_review(
    session,
) -> None:
    """Short summary style doesn't carry «uncertain» markers, so this
    check ALWAYS yields ~ — operator's manual review responsibility."""
    r = _row(zoom_id="zz31==")
    session.add(r); session.flush()
    check = _get_check(_checks(session, r), 20)
    assert check[2] == "~"


__all__ = []  # type: ignore[var-annotated]
