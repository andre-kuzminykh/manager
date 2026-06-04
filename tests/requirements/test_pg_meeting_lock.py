"""FR-CR-05-258 — per-meeting Postgres advisory lock (try_meeting_lock).

The runner (`ops/zoom_fireflies_runner.py`) and manual ops
(`ops/republish_meeting.py`) both call `try_meeting_lock` before
`process_one`. The lock serialises processing of one recording so a manual
republish firing during a cron tick can't double-post Slack/Doc/webhook.

These tests pin the contract without a live Postgres:
  - _lock_key is deterministic and fits a signed bigint.
  - distinct keys for distinct (source, source_id).
  - returns bool(scalar) when the query succeeds (acquired vs held).
  - best-effort: any DB error returns True so a lock-infra hiccup never
    blocks legitimate processing.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from app.services.pg_lock import _lock_key, try_meeting_lock


def test_lock_key_is_deterministic_and_signed_bigint():
    a = _lock_key("meeting:zoom:123")
    b = _lock_key("meeting:zoom:123")
    assert a == b
    # Postgres bigint range.
    assert -(2 ** 63) <= a < 2 ** 63


def test_lock_key_distinguishes_source_and_id():
    assert _lock_key("meeting:zoom:1") != _lock_key("meeting:fireflies:1")
    assert _lock_key("meeting:zoom:1") != _lock_key("meeting:zoom:2")


def _session_returning(value):
    sess = MagicMock()
    sess.execute.return_value.scalar.return_value = value
    return sess


def test_acquired_returns_true():
    sess = _session_returning(True)
    assert try_meeting_lock(sess, "zoom", "abc") is True
    # The crc32 key was passed as the bound :k parameter (positional).
    args, _ = sess.execute.call_args
    assert args[1] == {"k": _lock_key("meeting:zoom:abc")}


def test_held_by_other_returns_false():
    sess = _session_returning(False)
    assert try_meeting_lock(sess, "fireflies", "xyz") is False


def test_db_error_is_best_effort_true():
    sess = MagicMock()
    sess.execute.side_effect = RuntimeError("connection reset")
    # Never block legitimate processing on a lock-infra hiccup.
    assert try_meeting_lock(sess, "zoom", "abc") is True
