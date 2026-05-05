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
    assert res == []


def test_post_meeting_summary_no_op_when_channel_missing():
    res = post_meeting_summary_to_slack(
        slack_token="xoxb-test", channel_id="", body="hi",
    )
    assert res == []


def test_post_meeting_summary_no_op_when_body_blank():
    res = post_meeting_summary_to_slack(
        slack_token="xoxb-test", channel_id="C0XYZ", body="   ",
    )
    assert res == []


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

    # FR-CR-05-141 — list-of-responses, not single dict.
    assert isinstance(res, list)
    assert len(res) == 1
    assert res[0].get("ok") is True
    assert len(captured) == 1
    [call] = captured[0].calls
    assert call["channel"] == "D0AUXKND35Y"
    # Converted body uses Slack <url|label> form.
    assert "<https://docs.google.com/d/A/edit|30/04 - X>" in call["text"]
    # Don't auto-unfurl.
    assert call["unfurl_links"] is False
    assert call["unfurl_media"] is False


def test_post_meeting_summary_returns_empty_list_on_slack_api_error():
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
    assert res == []


def test_post_meeting_summary_returns_empty_list_on_unexpected_exception():
    class _BoomClient:
        def __init__(self, token):
            raise RuntimeError("transport down")

    with patch("slack_sdk.WebClient", side_effect=_BoomClient):
        res = post_meeting_summary_to_slack(
            slack_token="xoxb-test",
            channel_id="C0XYZ",
            body="hi",
        )
    assert res == []


# --- FR-CR-05-141 chunked delivery ---------------------------

def test_split_for_slack_short_body_one_chunk():
    from app.services.slack_mirror import _split_for_slack
    out = _split_for_slack("short body", limit=100)
    assert out == ["short body"]


def test_split_for_slack_paragraph_boundary():
    from app.services.slack_mirror import _split_for_slack
    body = "para1 long" * 10 + "\n\n" + "para2 long" * 10
    chunks = _split_for_slack(body, limit=120)
    assert len(chunks) == 2
    assert "para1" in chunks[0]
    assert "para2" in chunks[1]


def test_split_for_slack_handles_long_single_paragraph():
    from app.services.slack_mirror import _split_for_slack
    body = "line\n" * 200  # 1000 chars, no double-newlines
    chunks = _split_for_slack(body, limit=300)
    assert all(len(c) <= 300 for c in chunks)
    assert sum(len(c) for c in chunks) >= 950  # most content preserved


def test_post_meeting_summary_chunks_long_body_sequentially():
    """FR-CR-05-141 + FR-CR-05-149 — body > SLACK_TEXT_CHUNK_CHARS
    (3 500) splits into multiple chat.postMessage calls in order
    so we control the split boundaries instead of letting Slack
    auto-split mid-word."""
    from app.services.slack_mirror import SLACK_TEXT_CHUNK_CHARS

    captured: list[_FakeWebClient] = []

    def _factory(token):
        c = _FakeWebClient(token)
        captured.append(c)
        return c

    # Body of ~10 K chars (typical Fundraising sync size).
    long_body = (
        "Title\n\n"
        + "\n\n".join(f"Paragraph {i}: " + ("x" * 200) for i in range(50))
    )
    assert len(long_body) > SLACK_TEXT_CHUNK_CHARS

    with patch("slack_sdk.WebClient", side_effect=_factory):
        res = post_meeting_summary_to_slack(
            slack_token="xoxb-test",
            channel_id="D0AUXKND35Y",
            body=long_body,
        )

    assert isinstance(res, list)
    assert len(res) >= 3  # multiple chunks (10K / 3.5K ≈ 3)
    assert all(r.get("ok") for r in res)
    assert len(captured) == 1  # one client, multiple calls
    calls = captured[0].calls
    assert len(calls) == len(res)
    # Chunks delivered in order, each ≤ SLACK_TEXT_CHUNK_CHARS.
    for c in calls:
        assert len(c["text"]) <= SLACK_TEXT_CHUNK_CHARS
        assert c["channel"] == "D0AUXKND35Y"


def test_slack_chunk_limit_is_under_slack_auto_split_threshold():
    """FR-CR-05-149 — the chunk limit MUST be < ~4000 so that
    Slack server-side doesn't re-split our chunks mid-word.
    Operator regression: 10 730-char body with chunks_posted=1
    landed as 3 Slack messages with adjacent ts (3995/3974/2759
    chars), the second starting mid-task on «23) Update…».

    Pinning the upper bound here so a future tweak that bumps
    the limit (back to 35 000 or whatever) breaks this test
    loudly with a clear hint."""
    from app.services.slack_mirror import SLACK_TEXT_CHUNK_CHARS

    assert SLACK_TEXT_CHUNK_CHARS <= 3_900, (
        f"SLACK_TEXT_CHUNK_CHARS={SLACK_TEXT_CHUNK_CHARS} >= 4000 "
        "→ Slack will server-split mid-word; FR-CR-05-149 says "
        "keep it under 4000."
    )


# --- FR-CR-05-147 _compact_for_slack -------------------------


def test_compact_for_slack_collapses_blank_lines_between_numbered_tasks():
    """FR-CR-05-147 — operator regression «опять в слаке
    отдельные сообщения». Telegram body uses `\\n\\n` between
    every `1)`, `2)`, ... task (good for mobile spacing).
    Slack renders each `\\n\\n`-separated block as a separate
    bubble for long messages — looks like 60 floating cards.
    Compactor fuses consecutive numbered items into ONE
    paragraph."""
    from app.services.slack_mirror import _compact_for_slack

    body = (
        "30/04 - X meeting\n\n"
        "Участники: A, B\n\n"
        "Суть: short summary line\n\n"
        "To-Do:\n\n"
        "1) task one (Owner A)\n\n"
        "2) task two (Owner B)\n\n"
        "3) task three (Owner C)\n\n"
        "Подробный отчёт"
    )
    out = _compact_for_slack(body)
    # Header / Участники / Суть / To-Do header keep their
    # paragraph spacing.
    assert "30/04 - X meeting\n\nУчастники: A, B" in out
    assert "Участники: A, B\n\nСуть:" in out
    assert "Суть: short summary line\n\nTo-Do:" in out
    # First numbered item KEEPS the gap after «To-Do:».
    assert "To-Do:\n\n1) task one" in out
    # Numbered items 1) → 2) → 3) are fused with single newlines.
    assert "1) task one (Owner A)\n2) task two (Owner B)" in out
    assert "2) task two (Owner B)\n3) task three (Owner C)" in out
    # Trailing «Подробный отчёт» keeps its blank line.
    assert "(Owner C)\n\nПодробный отчёт" in out


def test_compact_for_slack_handles_two_digit_numbered_items():
    """`12) ...` and `100) ...` are still recognised as numbered
    list items (regex anchors on `\\d+\\)`)."""
    from app.services.slack_mirror import _compact_for_slack

    body = "To-Do:\n\n9) nine\n\n10) ten\n\n11) eleven"
    out = _compact_for_slack(body)
    assert out == "To-Do:\n\n9) nine\n10) ten\n11) eleven"


def test_compact_for_slack_collapses_excess_blank_runs():
    """3+ consecutive blank lines anywhere → max 2."""
    from app.services.slack_mirror import _compact_for_slack

    body = "Header\n\n\n\nBody\n\n\nFooter"
    out = _compact_for_slack(body)
    assert out == "Header\n\nBody\n\nFooter"


def test_compact_for_slack_no_op_when_no_numbered_list():
    """Body without numbered tasks passes through unchanged
    (apart from blank-run collapse)."""
    from app.services.slack_mirror import _compact_for_slack

    body = "Just a summary\n\nAnother paragraph"
    assert _compact_for_slack(body) == body


def test_compact_for_slack_empty_input_returns_empty():
    from app.services.slack_mirror import _compact_for_slack

    assert _compact_for_slack("") == ""


def test_post_meeting_summary_compacts_numbered_list_in_slack_call():
    """End-to-end: `post_meeting_summary_to_slack` runs the
    body through `_compact_for_slack` BEFORE chunk-splitting,
    so the actual Slack `chat.postMessage` text param has the
    numbered list compacted."""
    captured: list[_FakeWebClient] = []

    def _factory(token):
        c = _FakeWebClient(token)
        captured.append(c)
        return c

    with patch("slack_sdk.WebClient", side_effect=_factory):
        body = (
            "30/04 - sync\n\n"
            "Участники: A\n\n"
            "Суть: …\n\n"
            "To-Do:\n\n"
            "1) one (Owner)\n\n"
            "2) two (Owner)\n\n"
            "3) three (Owner)"
        )
        post_meeting_summary_to_slack(
            slack_token="xoxb-test", channel_id="D0AUXKND35Y",
            body=body,
        )
    [call] = captured[0].calls
    sent = call["text"]
    # Tasks fused — no blank line between consecutive items.
    assert "1) one (Owner)\n2) two (Owner)\n3) three (Owner)" in sent
    # Header → To-Do gap preserved.
    assert "Суть: …\n\nTo-Do:" in sent
