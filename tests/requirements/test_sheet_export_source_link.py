"""FR-CR-05-205 — strategic export sheet gets two context columns:
«Источник» (source channel) and «Ссылка» (link to the source / report).

Source + link come from the action-draft's `payload["_pending"]` provenance
block (source_kind + permalink), stamped at draft creation.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def test_fr_cr_05_205_headers_have_source_and_link() -> None:
    from app.sheet_sync.config import TASK_HEADERS

    assert "Источник" in TASK_HEADERS
    assert "Ссылка" in TASK_HEADERS
    assert len(TASK_HEADERS) == 16  # 14 base + 2 context


def test_fr_cr_05_205_telegram_draft_source_and_permalink() -> None:
    """Telegram draft → ('Telegram', message permalink) from _pending."""
    from ops.sheet_sync_export_tasks import _source_and_link

    payload = {
        "title": "X",
        "_pending": {
            "source_kind": "telegram",
            "permalink": "https://t.me/c/5288286547/240673",
            "conversation_id": "-5288286547",
        },
    }
    src, link = _source_and_link(MagicMock(), payload)
    assert src == "Telegram"
    assert link == "https://t.me/c/5288286547/240673"


def test_fr_cr_05_205_slack_draft_source_and_permalink() -> None:
    from ops.sheet_sync_export_tasks import _source_and_link

    payload = {
        "_pending": {
            "source_kind": "slack",
            "permalink": "https://slack.com/archives/C1/p123",
            "conversation_id": "C1",
        },
    }
    sess = MagicMock()
    src, link = _source_and_link(sess, payload)
    assert src == "Slack"
    assert link == "https://slack.com/archives/C1/p123"
    sess.query.assert_not_called()  # chat source must not hit recordings


def test_fr_cr_05_205_zoom_draft_prefers_doc_report() -> None:
    """Meeting-sourced draft (zoom) → ('Zoom', google_doc_url report)."""
    from ops.sheet_sync_export_tasks import _source_and_link

    payload = {
        "_pending": {
            "source_kind": "zoom",
            "permalink": "https://zoom.us/rec/share/abc",  # fallback, NOT used
            "conversation_id": "z1",
        },
    }
    sess = MagicMock()
    sess.query.return_value.filter.return_value.scalar.return_value = (
        "https://docs.google.com/document/d/REPORT/edit"
    )
    src, link = _source_and_link(sess, payload)
    assert src == "Zoom"
    assert link == "https://docs.google.com/document/d/REPORT/edit"


def test_fr_cr_05_205_no_pending_empty() -> None:
    from ops.sheet_sync_export_tasks import _source_and_link

    assert _source_and_link(MagicMock(), {"title": "X"}) == ("", "")
    assert _source_and_link(MagicMock(), {}) == ("", "")


# --- FR-CR-05-206 — strategic tasks from the `tasks` table (zoom/ff/slack) ---


def _task(*, kind: str, permalink: str | None, conversation_id: str | None):
    return SimpleNamespace(
        source_kind=SimpleNamespace(value=kind),
        source_permalink=permalink,
        source_conversation_id=conversation_id,
    )


def test_fr_cr_05_206_zoom_task_source_and_doc_report() -> None:
    """Zoom meeting task → ('Zoom', google_doc_url REPORT joined by zoom_id)."""
    from ops.sheet_sync_export_tasks import _source_and_link_for_task

    sess = MagicMock()
    sess.query.return_value.filter.return_value.scalar.return_value = (
        "https://docs.google.com/document/d/REPORT/edit"
    )
    task = _task(
        kind="zoom",
        permalink="https://zoom.us/rec/share/abc",  # fallback, NOT used
        conversation_id="z1",
    )
    src, link = _source_and_link_for_task(sess, task)
    assert src == "Zoom"
    assert link == "https://docs.google.com/document/d/REPORT/edit"


def test_fr_cr_05_206_fireflies_task_prefers_doc_report() -> None:
    from ops.sheet_sync_export_tasks import _source_and_link_for_task

    sess = MagicMock()
    sess.query.return_value.filter.return_value.scalar.return_value = (
        "https://docs.google.com/document/d/FF_REPORT/edit"
    )
    task = _task(kind="fireflies", permalink="https://ff/share/x", conversation_id="f1")
    src, link = _source_and_link_for_task(sess, task)
    assert src == "Fireflies"
    assert link == "https://docs.google.com/document/d/FF_REPORT/edit"


def test_fr_cr_05_206_slack_task_uses_permalink() -> None:
    """Chat task (slack) → permalink; must NOT hit the recordings tables."""
    from ops.sheet_sync_export_tasks import _source_and_link_for_task

    sess = MagicMock()
    task = _task(
        kind="slack",
        permalink="https://slack.com/archives/C1/p123",
        conversation_id="C1",
    )
    src, link = _source_and_link_for_task(sess, task)
    assert src == "Slack"
    assert link == "https://slack.com/archives/C1/p123"
    sess.query.assert_not_called()


def test_fr_cr_05_210_deadline_time_defaults_to_end_of_day() -> None:
    from ops.sheet_sync_export_tasks import _deadline_time

    # explicit time wins
    assert _deadline_time("2026-05-28", "09:30") == "09:30"
    # deadline date present, no time → 23:59 end-of-day default
    assert _deadline_time("2026-05-28", None) == "23:59"
    assert _deadline_time("2026-05-28", "") == "23:59"
    # no deadline date → empty (don't invent a time)
    assert _deadline_time("", None) == ""
    assert _deadline_time("", "23:59") == "23:59"  # explicit time still honored


def test_fr_cr_05_206_task_no_recording_falls_back_to_permalink() -> None:
    """Zoom task whose recording has no google_doc_url → keep source_permalink."""
    from ops.sheet_sync_export_tasks import _source_and_link_for_task

    sess = MagicMock()
    sess.query.return_value.filter.return_value.scalar.return_value = None
    task = _task(kind="zoom", permalink="https://zoom.us/rec/share/fallback", conversation_id="z9")
    src, link = _source_and_link_for_task(sess, task)
    assert src == "Zoom"
    assert link == "https://zoom.us/rec/share/fallback"
