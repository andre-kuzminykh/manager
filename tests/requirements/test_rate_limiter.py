"""Requirement coverage: NFR-CR-1 (rate-aware sender).

Unit tests for RateAwareSlackSender: throttling, 429 retry with
Retry-After, propagation of non-429 errors, and the shape of the
four wrappers (post_message / update_message / post_ephemeral /
delete_message)."""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from slack_sdk.errors import SlackApiError

from app.slack_bot.rate_limiter import RateAwareSlackSender


def _ok_response(data):
    r = MagicMock()
    r.data = data
    return r


def _rate_limit_error(retry_after: int = 1):
    resp = MagicMock()
    resp.status_code = 429
    resp.headers = {"Retry-After": str(retry_after)}
    err = SlackApiError("rate_limited", response=resp)
    return err


def _non_ratelimit_error():
    resp = MagicMock()
    resp.status_code = 500
    resp.headers = {}
    return SlackApiError("server error", response=resp)


# --------------------------------------------------------------------------- #
# Throttling (per-channel spacing)
# --------------------------------------------------------------------------- #


def test_throttle_enforces_min_interval_per_channel():
    client = MagicMock()
    client.chat_postMessage.return_value = _ok_response({"ok": True, "ts": "1.0"})
    sender = RateAwareSlackSender(client, min_interval_seconds=0.05)

    t0 = time.monotonic()
    sender.post_message(channel="C1", text="a")
    sender.post_message(channel="C1", text="b")
    elapsed = time.monotonic() - t0
    # Second call must wait at least one interval.
    assert elapsed >= 0.05


def test_throttle_is_per_channel_independent():
    client = MagicMock()
    client.chat_postMessage.return_value = _ok_response({"ok": True, "ts": "1.0"})
    sender = RateAwareSlackSender(client, min_interval_seconds=0.10)

    t0 = time.monotonic()
    sender.post_message(channel="C1", text="a")
    # Different channel — no throttle.
    sender.post_message(channel="C2", text="b")
    elapsed = time.monotonic() - t0
    assert elapsed < 0.09, f"expected near-zero wait, got {elapsed}"


# --------------------------------------------------------------------------- #
# 429 retry with Retry-After
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "method,method_kwargs,sdk_attr",
    [
        ("post_message", {"channel": "C1", "text": "x"}, "chat_postMessage"),
        ("update_message", {"channel": "C1", "ts": "1.0", "text": "x"}, "chat_update"),
        ("post_ephemeral", {"channel": "C1", "user": "U1", "text": "x"}, "chat_postEphemeral"),
        ("delete_message", {"channel": "C1", "ts": "1.0"}, "chat_delete"),
    ],
)
def test_429_retries_then_succeeds(method, method_kwargs, sdk_attr, monkeypatch):
    client = MagicMock()
    sdk_method = getattr(client, sdk_attr)
    sdk_method.side_effect = [
        _rate_limit_error(retry_after=0),  # retry_after=0 to keep the test fast
        _ok_response({"ok": True, "ts": "1.0"}),
    ]
    # Avoid sleeping for real.
    monkeypatch.setattr("app.slack_bot.rate_limiter.time.sleep", lambda *_: None)
    sender = RateAwareSlackSender(client, min_interval_seconds=0)

    out = getattr(sender, method)(**method_kwargs)
    assert out == {"ok": True, "ts": "1.0"}
    assert sdk_method.call_count == 2


def test_429_gives_up_after_three_attempts(monkeypatch):
    client = MagicMock()
    client.chat_postMessage.side_effect = _rate_limit_error(0)
    monkeypatch.setattr("app.slack_bot.rate_limiter.time.sleep", lambda *_: None)
    sender = RateAwareSlackSender(client, min_interval_seconds=0)

    with pytest.raises(SlackApiError):
        sender.post_message(channel="C1", text="x")
    # Initial + 3 retries = 4 attempts.
    assert client.chat_postMessage.call_count == 4


def test_non_429_error_propagates_without_retry(monkeypatch):
    client = MagicMock()
    client.chat_postMessage.side_effect = _non_ratelimit_error()
    monkeypatch.setattr("app.slack_bot.rate_limiter.time.sleep", lambda *_: None)
    sender = RateAwareSlackSender(client, min_interval_seconds=0)

    with pytest.raises(SlackApiError):
        sender.post_message(channel="C1", text="x")
    assert client.chat_postMessage.call_count == 1


# --------------------------------------------------------------------------- #
# Kwargs pass-through
# --------------------------------------------------------------------------- #


def test_post_message_passes_kwargs_through_to_sdk():
    client = MagicMock()
    client.chat_postMessage.return_value = _ok_response({"ok": True})
    sender = RateAwareSlackSender(client, min_interval_seconds=0)

    sender.post_message(channel="C1", text="hi", blocks=[{"type": "section"}], thread_ts="10.0")
    call = client.chat_postMessage.call_args.kwargs
    assert call["channel"] == "C1"
    assert call["text"] == "hi"
    assert call["blocks"] == [{"type": "section"}]
    assert call["thread_ts"] == "10.0"


def test_update_message_routes_to_chat_update():
    client = MagicMock()
    client.chat_update.return_value = _ok_response({"ok": True})
    sender = RateAwareSlackSender(client, min_interval_seconds=0)

    sender.update_message(channel="C1", ts="1.0", text="new")
    client.chat_update.assert_called_once()


def test_delete_message_routes_to_chat_delete():
    client = MagicMock()
    client.chat_delete.return_value = _ok_response({"ok": True})
    sender = RateAwareSlackSender(client, min_interval_seconds=0)

    sender.delete_message(channel="C1", ts="1.0")
    client.chat_delete.assert_called_once_with(channel="C1", ts="1.0")
