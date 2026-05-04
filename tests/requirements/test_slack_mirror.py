"""FR-CR-05-137 — Slack mirror for Zoom meeting summaries.

Covers:
- HTML→Slack-mrkdwn conversion (`<a href>` → `<url|label>`).
- `post_meeting_summary_to_slack` no-op when token / channel /
  body missing — never raises.
- The function delegates to `slack_sdk.WebClient.chat_postMessage`
  with the right channel + converted body + unfurl flags off.
- Slack 5xx / network errors are swallowed (returns None).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.slack_mirror import (
    _to_slack_mrkdwn,
    post_meeting_summary_to_slack,
)


# --- _to_slack_mrkdwn -----------------------------------------

def test_to_slack_mrkdwn_swaps_anchor_to_pipe_form():
    body = (
        '<a href="https://docs.google.com/document/d/abc/edit">'
        '30/04 - US Innovative Technology</a>\n\n'
        'Участники: Алина, Артем\n\nТо-Do:\n1) Подготовить deck'
    )
    out = _to_slack_mrkdwn(body)
    assert (
        "<https://docs.google.com/document/d/abc/edit|"
        "30/04 - US Innovative Technology>"
    ) in out
    assert "<a href" not in out
    assert "Участники: Алина" in out


def test_to_slack_mrkdwn_decodes_html_entities():
    body = '<a href="x">A &amp; B</a> &lt;hi&gt; &quot;q&quot;'
    out = _to_slack_mrkdwn(body)
    assert "A & B" in out
    assert "<hi>" in out
    assert '"q"' in out
    assert "&amp;" not in out


def test_to_slack_mrkdwn_no_anchor_pass_through():
    body = "plain body\n\nwith newlines"
    assert _to_slack_mrkdwn(body) == body


def test_to_slack_mrkdwn_empty_string_returns_empty():
    assert _to_slack_mrkdwn("") == ""


# --- post_meeting_summary_to_slack ----------------------------

class _FakeWebClient:
    def __init__(self, token):
        self.token = token
        self.calls = []

    def chat_postMessage(self, **kwargs):  # noqa: N802
        self.calls.append(kwargs)
        return SimpleNamespace(data={"ok": True, "ts": "1700000000.000001"})


def test_post_meeting_summary_no_op_when_token_missing():
    res = post_meeting_summary_to_slack(
        slack_token="", channel_id="C0XYZ", body="hi",
    )
    assert res is None


def test_post_meeting_summary_no_op_when_channel_missing():
    res = post_meeting_summary_to_slack(
        slack_token="xoxb-test", channel_id="", body="hi",
    )
    assert res is None


def test_post_meeting_summary_no_op_when_body_blank():
    res = post_meeting_summary_to_slack(
        slack_token="xoxb-test", channel_id="C0XYZ", body="   ",
    )
    assert res is None


def test_post_meeting_summary_calls_chat_postMessage_with_converted_body():
    captured: list[_FakeWebClient] = []

    def _factory(token):
        c = _FakeWebClient(token)
        captured.append(c)
        return c

    with patch("slack_sdk.WebClient", side_effect=_factory):
        body = '<a href="https://docs.google.com/d/A/edit">30/04 - X</a>\n\nbody'
        res = post_meeting_summary_to_slack(
            slack_token="xoxb-test",
            channel_id="D0AUXKND35Y",  # Artem AI bot DM
            body=body,
        )

    assert res is not None
    assert res.get("ok") is True
    assert len(captured) == 1
    [call] = captured[0].calls
    assert call["channel"] == "D0AUXKND35Y"
    # Converted body uses Slack <url|label> form.
    assert "<https://docs.google.com/d/A/edit|30/04 - X>" in call["text"]
    # Don't auto-unfurl.
    assert call["unfurl_links"] is False
    assert call["unfurl_media"] is False


def test_post_meeting_summary_returns_none_on_slack_api_error():
    """Slack rate-limit / 5xx etc. — caller treats as non-fatal,
    pipeline keeps going."""
    from slack_sdk.errors import SlackApiError

    class _ErroringClient:
        def __init__(self, token):
            self.token = token

        def chat_postMessage(self, **kwargs):  # noqa: N802
            err_resp = SimpleNamespace(
                status_code=500,
                data={"error": "internal_error"},
                headers={},
            )
            raise SlackApiError(message="boom", response=err_resp)

    with patch("slack_sdk.WebClient", side_effect=_ErroringClient):
        res = post_meeting_summary_to_slack(
            slack_token="xoxb-test",
            channel_id="C0XYZ",
            body="hi",
        )
    assert res is None


def test_post_meeting_summary_returns_none_on_unexpected_exception():
    class _BoomClient:
        def __init__(self, token):
            raise RuntimeError("transport down")

    with patch("slack_sdk.WebClient", side_effect=_BoomClient):
        res = post_meeting_summary_to_slack(
            slack_token="xoxb-test",
            channel_id="C0XYZ",
            body="hi",
        )
    assert res is None
