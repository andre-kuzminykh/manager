"""FR-CR-05-160 — ID-locked tests for the external meeting webhook
(n8n consumer). Contract:

  - Empty `webhook_url` → no-op, returns False, NO network call.
  - Empty / whitespace `short_summary` → no-op, returns False, NO call
    (we never push empty summaries to the consumer).
  - Valid call → POSTs JSON to the URL, returns True on 2xx.
  - Payload carries the agreed fields. `tasks_count` is an INT (a
    count), NOT the task list — the consumer side gets a number, not
    titles/owners/due. Locked here so a future change that needs the
    actual task list is forced to update this contract deliberately.
  - HTTP / transport errors are swallowed (logged), return False,
    never raise — a webhook outage must not break the pipeline.

Regression context (2026-06-03): the webhook silently went dark for
weeks because MEETING_WEBHOOK_URL was missing on the live deployment.
These tests pin the *delivery* contract; the *observability* that
surfaces a missing URL lives in the pipeline call sites (see
`meeting_webhook_skipped_no_url` log) and `test_*_pipeline` coverage.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from app.services.meeting_webhook import post_meeting_to_webhook

_URL = "https://example.invalid/webhook/test"


def _kwargs(**over):
    base = dict(
        webhook_url=_URL,
        source="zoom",
        source_id="zoom-1",
        title="Weekly sync",
        meeting_date=datetime(2026, 6, 3, 11, 30, tzinfo=timezone.utc),
        duration_seconds=1800,
        short_summary="Обсудили статус.",
        detailed_summary="Длинное саммари.",
        google_doc_url="https://docs/x",
        participants=["Andre", "Дмитрий"],
        tasks_count=3,
    )
    base.update(over)
    return base


def _mock_urlopen(status: int = 200) -> MagicMock:
    """Build a urlopen mock usable as a context manager."""
    resp = MagicMock()
    resp.status = status
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=resp)
    cm.__exit__ = MagicMock(return_value=False)
    return MagicMock(return_value=cm)


# --------------------------------------------------------------------------- #
# Gating — no-op cases MUST NOT touch the network
# --------------------------------------------------------------------------- #


def test_fr_cr_05_160_empty_url_is_noop() -> None:
    with patch("urllib.request.urlopen") as uo:
        out = post_meeting_to_webhook(**_kwargs(webhook_url=""))
    assert out is False
    uo.assert_not_called()


def test_fr_cr_05_160_empty_summary_is_noop() -> None:
    with patch("urllib.request.urlopen") as uo:
        out = post_meeting_to_webhook(**_kwargs(short_summary="   "))
    assert out is False
    uo.assert_not_called()


def test_fr_cr_05_160_none_summary_is_noop() -> None:
    with patch("urllib.request.urlopen") as uo:
        out = post_meeting_to_webhook(**_kwargs(short_summary=None))
    assert out is False
    uo.assert_not_called()


# --------------------------------------------------------------------------- #
# Happy path — POST fires, payload shape locked
# --------------------------------------------------------------------------- #


def test_fr_cr_05_160_posts_and_returns_true_on_2xx() -> None:
    uo = _mock_urlopen(200)
    with patch("urllib.request.urlopen", uo):
        out = post_meeting_to_webhook(**_kwargs())
    assert out is True
    uo.assert_called_once()


def test_fr_cr_05_160_payload_shape_locked() -> None:
    captured = {}

    uo = _mock_urlopen(200)

    def _capture(req, *a, **k):  # noqa: ANN001
        captured["data"] = req.data
        captured["url"] = req.full_url
        captured["ctype"] = req.headers.get("Content-type")
        return uo.return_value

    with patch("urllib.request.urlopen", side_effect=_capture):
        post_meeting_to_webhook(**_kwargs())

    assert captured["url"] == _URL
    payload = json.loads(captured["data"].decode("utf-8"))
    # Exact key set — adding/removing a field is a deliberate contract change.
    assert set(payload) == {
        "source", "source_id", "title", "meeting_date",
        "duration_seconds", "short_summary", "detailed_summary",
        "google_doc_url", "participants", "tasks_count",
    }
    # tasks_count is a COUNT (int), not the task list.
    assert payload["tasks_count"] == 3
    assert isinstance(payload["tasks_count"], int)
    assert payload["participants"] == ["Andre", "Дмитрий"]
    # UTF-8, non-ASCII preserved (ensure_ascii=False).
    assert "Дмитрий".encode("utf-8") in captured["data"]


# --------------------------------------------------------------------------- #
# Failure modes — swallowed, never raise
# --------------------------------------------------------------------------- #


def test_fr_cr_05_160_http_error_returns_false_no_raise() -> None:
    import urllib.error

    def _raise(*a, **k):  # noqa: ANN001
        raise urllib.error.HTTPError(_URL, 500, "boom", {}, None)  # type: ignore[arg-type]

    with patch("urllib.request.urlopen", side_effect=_raise):
        out = post_meeting_to_webhook(**_kwargs())
    assert out is False


def test_fr_cr_05_160_transport_error_returns_false_no_raise() -> None:
    def _raise(*a, **k):  # noqa: ANN001
        raise OSError("connection refused")

    with patch("urllib.request.urlopen", side_effect=_raise):
        out = post_meeting_to_webhook(**_kwargs())
    assert out is False


def test_fr_cr_05_160_non_2xx_returns_false() -> None:
    uo = _mock_urlopen(404)
    with patch("urllib.request.urlopen", uo):
        out = post_meeting_to_webhook(**_kwargs())
    assert out is False


__all__: list[str] = []
