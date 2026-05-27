"""FR-CR-05-205 — strategic export sheet gets two context columns:
«Источник» (source channel) and «Ссылка» (link to the source / report).

Source + link come from the action-draft's `payload["_pending"]` provenance
block (source_kind + permalink), stamped at draft creation.
"""
from __future__ import annotations

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
