"""FR-CR-05-89 — `ops/resync_sheet.py` bulk re-sync CLI.

Operator workflow: dropped all rows in the spreadsheet,
needs the bot to re-emit each task into a clean A:V layout
(post-FR-CR-05-86) and renormalise legacy long titles
(post-FR-CR-05-72/-89).
"""
from __future__ import annotations

import pytest

from app.models import GoogleSheetsSync, Task, TaskStatus
from app.models.task import TaskPriority
from app.persistence.tasks import normalize_task_title


def test_normalize_task_title_caps_long_no_break_paragraph():
    """The FR-CR-05-89 backstop: even a single 200-char
    paragraph with no early `:` / ` — ` / `; ` / `. ` separator
    must come out ≤101 chars (100 + the ellipsis). Without
    this, screenshot transcriptions slipped past the
    clause-break loop and shipped a wall-of-text title."""
    long_paragraph = (
        "На изображении показано электронное письмо от Артема "
        "Соколова отправленное Джоди и с копией Ирине и ещё одному "
        "адресату содержащее приглашение на встречу"
    )
    out = normalize_task_title(long_paragraph)
    # 100-char hard cap plus a single ellipsis.
    assert len(out) <= 101
    assert out.endswith("…")
    # Word boundary cut, not mid-word.
    assert not out[:-1].endswith(" ")


def test_normalize_task_title_preserves_short_titles_unchanged():
    """Short titles pass through capitalised and unchanged
    otherwise — the helper must not rewrite content."""
    out = normalize_task_title("подготовить отчёт")
    assert out == "Подготовить отчёт"


def test_normalize_task_title_first_clause_break_wins_over_hard_cut():
    """A long source with an early `:` / ` — ` keeps the
    first clause as the title, instead of falling through to
    the hard cut + ellipsis. Length ≥101 to trigger the
    clause-break loop."""
    long_with_break = (
        "Поговорил с Fortuna: 1) по SPAC — обсудили условия "
        "и сроки; 2) по due-dil — нужно ответить инвестору в "
        "понедельник к 12:00 МСК"
    )
    assert len(long_with_break) > 100
    out = normalize_task_title(long_with_break)
    assert out == "Поговорил с Fortuna"


def test_resync_sheet_dry_run_reports_capped_titles_without_writing(
    patched_session_scope, SessionFactory, monkeypatch
):
    """`--dry-run` lists how many titles would be capped and
    rows reset, without mutating DB state or calling
    `sheets.sync`."""
    import ops.resync_sheet as mod

    long_paragraph = (
        "На изображении показано электронное письмо от Артема "
        "Соколова отправленное Джоди и с копией Ирине и ещё одному "
        "адресату содержащее приглашение на встречу"
    )
    assert len(long_paragraph) > 100
    with SessionFactory() as s:
        t = Task(
            title=long_paragraph,
            owner_user_id="111",
            priority=TaskPriority.medium,
            status=TaskStatus.todo,
            google_sheets_row_id=42,
        )
        s.add(t)
        s.flush()
        s.add(GoogleSheetsSync(task_id=t.id, spreadsheet_id="s", row_id=42))
        s.commit()

    sync_calls: list[int] = []

    class _StubSheets:
        def sync(self, session, task):
            sync_calls.append(task.id)

    monkeypatch.setattr(
        mod, "build_sheets_factory", lambda settings: lambda: _StubSheets()
    )
    monkeypatch.setattr(mod, "session_scope", patched_session_scope)
    monkeypatch.setattr(mod.sys, "argv", ["ops.resync_sheet", "--dry-run"])
    rc = mod.main()
    assert rc == 0
    # Dry-run never calls sync.
    assert sync_calls == []
    # And never mutates the title or the row pointer.
    with SessionFactory() as s:
        row = s.query(Task).one()
        assert row.title == long_paragraph
        assert row.google_sheets_row_id == 42


def test_resync_sheet_caps_titles_resets_row_id_and_resyncs(
    patched_session_scope, SessionFactory, monkeypatch
):
    """Wet-run: legacy long title gets normalised, the
    legacy row pointer is cleared (so the next sync appends
    fresh into A:V — FR-CR-05-86 fix path), and `sheets.sync`
    is called once per task."""
    import ops.resync_sheet as mod

    long_paragraph = (
        "На изображении показано электронное письмо от Артема "
        "Соколова отправленное Джоди и с копией Ирине и ещё одному "
        "адресату содержащее приглашение на встречу"
    )
    assert len(long_paragraph) > 100
    with SessionFactory() as s:
        t = Task(
            title=long_paragraph,
            owner_user_id="111",
            priority=TaskPriority.medium,
            status=TaskStatus.todo,
            google_sheets_row_id=42,
        )
        s.add(t)
        s.flush()
        s.add(GoogleSheetsSync(task_id=t.id, spreadsheet_id="s", row_id=42))
        # Soft-deleted task — excluded by default.
        td = Task(
            title="x",
            owner_user_id="111",
            priority=TaskPriority.medium,
            status=TaskStatus.todo,
        )
        td.deleted_at = __import__("datetime").datetime(
            2026, 4, 30, tzinfo=__import__("datetime").timezone.utc
        )
        s.add(td)
        s.commit()

    sync_calls: list[int] = []

    class _StubSheets:
        def sync(self, session, task):
            sync_calls.append(task.id)

    monkeypatch.setattr(
        mod, "build_sheets_factory", lambda settings: lambda: _StubSheets()
    )
    monkeypatch.setattr(mod, "session_scope", patched_session_scope)
    monkeypatch.setattr(mod.sys, "argv", ["ops.resync_sheet"])
    rc = mod.main()
    assert rc == 0
    # Synced one task — the soft-deleted one is excluded.
    assert len(sync_calls) == 1
    with SessionFactory() as s:
        row = s.query(Task).filter(Task.deleted_at.is_(None)).one()
        # Title got normalised down to ≤101 chars.
        assert len(row.title) <= 101
        assert row.title != long_paragraph
        # The legacy row pointer is cleared.
        assert row.google_sheets_row_id is None
        # And the matching GoogleSheetsSync row likewise.
        gs = s.query(GoogleSheetsSync).filter_by(task_id=row.id).one()
        assert gs.row_id is None


def test_resync_sheet_include_deleted_flag_pushes_tombstones(
    patched_session_scope, SessionFactory, monkeypatch
):
    """`--include-deleted` re-pushes soft-deleted rows so the
    sheet shows their `status=deleted` tombstone (per
    FR-CR-04-23 row layout). Useful after a manual sheet
    purge."""
    import ops.resync_sheet as mod

    with SessionFactory() as s:
        td = Task(
            title="x",
            owner_user_id="111",
            priority=TaskPriority.medium,
            status=TaskStatus.todo,
        )
        td.deleted_at = __import__("datetime").datetime(
            2026, 4, 30, tzinfo=__import__("datetime").timezone.utc
        )
        s.add(td)
        s.commit()

    sync_calls: list[int] = []

    class _StubSheets:
        def sync(self, session, task):
            sync_calls.append(task.id)

    monkeypatch.setattr(
        mod, "build_sheets_factory", lambda settings: lambda: _StubSheets()
    )
    monkeypatch.setattr(mod, "session_scope", patched_session_scope)
    monkeypatch.setattr(
        mod.sys, "argv", ["ops.resync_sheet", "--include-deleted"]
    )
    rc = mod.main()
    assert rc == 0
    assert len(sync_calls) == 1


def test_resync_sheet_returns_exit_code_2_when_no_credentials(monkeypatch):
    """Bad config → exit 2 so the cron caller knows to alert."""
    import ops.resync_sheet as mod

    monkeypatch.setattr(
        mod, "build_sheets_factory", lambda settings: None
    )
    monkeypatch.setattr(mod.sys, "argv", ["ops.resync_sheet"])
    rc = mod.main()
    assert rc == 2
