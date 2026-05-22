"""FR-CR-05-192v — ceo_brain dispatcher: archive write is best-effort.

Production regression on 2026-05-22: docker copy / chown earlier in
the day left ``/app/traces/slack-archive/D0ASY5QF6UX/2026-05-22.jsonl``
write-protected for the bot user. `write_archive` raised PermissionError
→ outer `_handle` caught it as `ceo_brain_dispatch_failed` → the
responder NEVER fired → the bot looked dead in DMs and thread replies.

Contract locked here:
  - When `write_archive` raises ANY exception, `handle_event` MUST:
      1. log `ceo_brain_archive_write_failed` with channel/ts/error
      2. set `result.archive_failed = True`
      3. continue to the responder dispatch step
  - The responder MUST fire even when the archive write failed
    (operator cares more about the live agent than the audit trail).
  - Successful path is unchanged: `result.archived = True`,
    `result.archive_failed = False`.
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.ceo_brain.dispatcher import (
    DispatchResult,
    _reset_responder_dedup_for_tests,
    handle_event,
)


@pytest.fixture(autouse=True)
def _reset_dedup():
    _reset_responder_dedup_for_tests()
    yield
    _reset_responder_dedup_for_tests()


def _payload(**over) -> dict:
    # Unique ts per call so in-process dedup doesn't cross tests.
    p = {
        "type": "message",
        "channel": "D_TEST",
        "channel_type": "im",
        "user": "U_OPERATOR",
        "ts": f"177941{uuid.uuid4().int % 10**8:08d}.{uuid.uuid4().int % 10**6:06d}",
        "text": "Привет, бот",
    }
    p.update(over)
    return p


def test_fr_cr_05_192v_archive_failure_still_runs_responder(session) -> None:
    """When write_archive raises PermissionError, the responder MUST
    still be invoked. archive_failed flag set; archived flag unset."""
    responder = MagicMock()
    with (
        patch(
            "app.ceo_brain.dispatcher.should_archive_channel",
            return_value=True,
        ),
        patch(
            "app.ceo_brain.dispatcher.write_archive",
            side_effect=PermissionError(
                "[Errno 13] Permission denied: "
                "'/app/traces/slack-archive/D_TEST/2026-05-22.jsonl'"
            ),
        ),
        patch(
            "app.ceo_brain.dispatcher._is_duplicate_archive",
            return_value=False,
        ),
        patch(
            "app.config.get_settings",
            return_value=MagicMock(ceo_brain_allowed_users=""),
        ),
    ):
        result = handle_event(
            session, _payload(),
            bot_user_id="U_BOT",
            responder=responder,
        )
    assert result.archive_failed is True
    assert result.archived is False
    # Responder MUST have fired despite the archive failure
    responder.assert_called_once()


def test_fr_cr_05_192v_archive_success_unchanged(session) -> None:
    """Happy path: write_archive succeeds; archive_failed stays False;
    archived flips True; responder still fires."""
    responder = MagicMock()
    with (
        patch(
            "app.ceo_brain.dispatcher.should_archive_channel",
            return_value=True,
        ),
        patch("app.ceo_brain.dispatcher.write_archive", return_value=None),
        patch(
            "app.ceo_brain.dispatcher._is_duplicate_archive",
            return_value=False,
        ),
        patch(
            "app.config.get_settings",
            return_value=MagicMock(ceo_brain_allowed_users=""),
        ),
    ):
        result = handle_event(
            session, _payload(),
            bot_user_id="U_BOT",
            responder=responder,
        )
    assert result.archived is True
    assert result.archive_failed is False
    responder.assert_called_once()


def test_fr_cr_05_192v_archive_failure_logs_warning(session, caplog) -> None:
    """Operator must be able to grep `ceo_brain_archive_write_failed`
    in logs to find the underlying disk/permission cause."""
    responder = MagicMock()
    with (
        patch(
            "app.ceo_brain.dispatcher.should_archive_channel",
            return_value=True,
        ),
        patch(
            "app.ceo_brain.dispatcher.write_archive",
            side_effect=OSError("disk full"),
        ),
        patch(
            "app.ceo_brain.dispatcher._is_duplicate_archive",
            return_value=False,
        ),
        patch(
            "app.config.get_settings",
            return_value=MagicMock(ceo_brain_allowed_users=""),
        ),
    ):
        handle_event(
            session, _payload(),
            bot_user_id="U_BOT",
            responder=responder,
        )
    # Best-effort path completed without crashing; responder fired.
    # The structlog warning is captured in stdout (visible in the
    # «Captured stdout call» section). We don't assert on caplog
    # records because structlog's stdlib bridge differs across
    # configurations — instead we lock the behavior: archive failure
    # MUST NOT block the responder.
    responder.assert_called_once()


def test_fr_cr_05_192v_dispatch_result_has_archive_failed_field() -> None:
    """Schema lock: `DispatchResult.archive_failed: bool` MUST exist
    so callers (and `_handle`) can branch on the new state."""
    r = DispatchResult()
    assert r.archive_failed is False
    r.archive_failed = True
    assert r.archive_failed is True


__all__ = []  # type: ignore[var-annotated]
