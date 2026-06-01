"""FR-CR-05-234 — re-extract Slack tasks → sheet (safety + row shape)."""
from __future__ import annotations

import pathlib

import ops.slack_reextract_to_sheet as rx

_SRC = pathlib.Path(rx.__file__).read_text(encoding="utf-8")


def test_row_shape_matches_headers_and_marks_slack():
    from app.sheet_sync.config import TASK_HEADERS

    row = rx._row(
        title="Do X", description="ctx", owner="Ilia Martynov",
        priority_key="urgent", direction="investors", due="2026-05-29",
        added_at="2026-05-29 11:00", link="https://slack/...",
    )
    assert len(row) == len(TASK_HEADERS) == 16
    assert row[0] == "Do X"
    assert row[2] == "Ilia Martynov"          # owner shown (clean name)
    assert row[4] == "High"                    # urgent → High display
    assert row[5] == "Investors"               # direction capitalised
    assert row[8] == "2026-05-29" and row[9] == "23:59"  # deadline + EOD
    assert row[13] == "2026-05-29 11:00"       # real message time
    assert row[14] == "Slack"                  # Источник


def test_deadline_time_eod_only_when_due_present():
    assert rx._deadline_time("2026-05-29") == "23:59"
    assert rx._deadline_time("") == ""


def test_is_read_only_and_dry_run_by_default():
    # DB is never mutated: a rollback guards the session, no add/commit of
    # action_drafts / tasks anywhere in this op.
    assert "session.rollback()" in _SRC
    assert "create_task_from_draft" not in _SRC
    assert "create_draft" not in _SRC
    # writing requires explicit --apply
    assert '"--apply"' in _SRC
    assert "if not args.apply:" in _SRC
    # only the Slack section is rewritten
    assert "delete_rows_where_source(_SLACK_SRC)" in _SRC
    assert '_SLACK_SRC = "Slack"' in _SRC
