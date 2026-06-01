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


def test_humanize_mentions_keeps_jira_keys():
    m = {"U0796A349R7": "Polly Ng"}
    assert rx._humanize("Transfer ownership to U0796A349R7", m) == "Transfer ownership to Polly Ng"
    assert rx._humanize("share with <@U0796A349R7> today", m) == "share with Polly Ng today"
    # unknown uid is left as-is; Jira keys (HUMPROC-####) are not touched
    assert rx._humanize("Provide update on HUMPROC-4335", m) == "Provide update on HUMPROC-4335"


def test_noise_reason_flags_non_tasks_but_keeps_imperatives():
    assert rx._noise_reason("*Weekly Hiring Update* (May 18-22)\n\nHi all") == "multiline-paste"
    assert rx._noise_reason("Hi, we are currently arranging the logistics") == "greeting"
    assert rx._noise_reason("When we last extended our London WeWork contract") == "question-start"
    assert rx._noise_reason("Какие задачи открыты?") == "question"
    # real imperative tasks survive
    for t in ["Review and sign the NDA", "Provide update on HUMPROC-4335",
              "Reach out to Amazon to unblock account", "Issue 3 new credit cards"]:
        assert rx._noise_reason(t) is None, t


def test_clean_rows_drops_noise_and_fixes_names():
    m = {"U0796A349R7": "Polly Ng"}
    rows = [
        rx._row(title="Send signed agreement to U0796A349R7", description="", owner="X",
                priority_key="medium", direction="other", due="", added_at="", link=""),
        rx._row(title="*Weekly Hiring Update*\nHi all", description="", owner="X",
                priority_key="medium", direction="other", due="", added_at="", link=""),
    ]
    out = rx._clean_rows(rows, m)
    assert len(out) == 1
    assert out[0][0] == "Send signed agreement to Polly Ng"


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
