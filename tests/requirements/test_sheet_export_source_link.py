"""FR-CR-05-205 — strategic export sheet gets two context columns:
«Источник» (source channel) and «Ссылка» (link to the meeting report).
"""
from __future__ import annotations

from unittest.mock import MagicMock


def test_fr_cr_05_205_headers_have_source_and_link() -> None:
    from app.sheet_sync.config import TASK_HEADERS

    assert "Источник" in TASK_HEADERS
    assert "Ссылка" in TASK_HEADERS
    assert len(TASK_HEADERS) == 16  # 14 base + 2 context


def test_fr_cr_05_205_zoom_task_links_to_doc_report() -> None:
    """Zoom task → ('Zoom', google_doc_url of the recording report)."""
    from ops.sheet_sync_export_tasks import _source_and_link

    task = MagicMock()
    task.source_kind.value = "zoom"
    task.source_conversation_id = "z1"
    task.source_permalink = "https://zoom.us/rec/share/abc"  # fallback, NOT used
    sess = MagicMock()
    sess.get.return_value = task
    sess.query.return_value.filter.return_value.scalar.return_value = (
        "https://docs.google.com/document/d/REPORT/edit"
    )

    src, link = _source_and_link(sess, 42)
    assert src == "Zoom"
    assert link == "https://docs.google.com/document/d/REPORT/edit"


def test_fr_cr_05_205_zoom_falls_back_to_permalink_when_no_doc() -> None:
    """Zoom task with no doc on the recording → source_permalink."""
    from ops.sheet_sync_export_tasks import _source_and_link

    task = MagicMock()
    task.source_kind.value = "zoom"
    task.source_conversation_id = "z1"
    task.source_permalink = "https://zoom.us/rec/share/abc"
    sess = MagicMock()
    sess.get.return_value = task
    sess.query.return_value.filter.return_value.scalar.return_value = None

    src, link = _source_and_link(sess, 42)
    assert src == "Zoom"
    assert link == "https://zoom.us/rec/share/abc"


def test_fr_cr_05_205_slack_task_uses_permalink() -> None:
    """Slack task → ('Slack', source_permalink); no recording lookup."""
    from ops.sheet_sync_export_tasks import _source_and_link

    task = MagicMock()
    task.source_kind.value = "slack"
    task.source_conversation_id = "C1"
    task.source_permalink = "https://slack.com/archives/C1/p123"
    sess = MagicMock()
    sess.get.return_value = task

    src, link = _source_and_link(sess, 7)
    assert src == "Slack"
    assert link == "https://slack.com/archives/C1/p123"
    sess.query.assert_not_called()  # slack must not hit recordings


def test_fr_cr_05_205_no_task_id_empty() -> None:
    from ops.sheet_sync_export_tasks import _source_and_link

    assert _source_and_link(MagicMock(), None) == ("", "")
