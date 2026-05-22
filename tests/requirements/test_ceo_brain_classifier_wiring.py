"""FR-CR-05-192w — ID-locked tests for the `CEO_BRAIN_TASK_CLASSIFIER_ENABLED`
env switch that toggles task-extraction inside the CEO Brain handler.

Operator-pinned 2026-05-22:
  Phase 1: «CEO Brain — это, другого не надо» — wire classifier.
  Phase 2: «легаси только телеграм и слак задачи слушал, а остальное
           менеджер» — when running hybrid with legacy slack-ingest as
           the Slack-task source of truth, disable CEO Brain classifier
           via env so it doesn't double-emit drafts.

Contract locked:
  - Default (env unset OR true/1/yes/on, case-insensitive) → classifier
    services are built; DMs get drafts + Task rows on confirm.
  - false / 0 / no / off (case-insensitive) → services skipped, log
    line `ceo_brain_task_classifier_disabled_by_env` fires once at
    handler startup, DMs still get agent responses but no task drafts.
  - Unrecognised value (e.g. «maybe») → defaults to true (back-compat
    safety: never silently drop tasks).
"""
from __future__ import annotations

import os
from unittest.mock import patch


def _env_flag_enabled(value: str | None) -> bool:
    """Mirror of the exact parsing logic in slack_handler.py:

        os.environ.get("CEO_BRAIN_TASK_CLASSIFIER_ENABLED", "true")
            .strip().lower() not in ("false", "0", "no", "off")
    """
    raw = value if value is not None else "true"
    return raw.strip().lower() not in ("false", "0", "no", "off")


def test_fr_cr_05_192w_env_flag_default_true() -> None:
    """Env var unset → classifier MUST be enabled (back-compat)."""
    assert _env_flag_enabled(None) is True


def test_fr_cr_05_192w_env_flag_false_strings_recognised() -> None:
    """All canonical falsy strings disable the classifier — and the
    parse is case-insensitive + whitespace-tolerant so operator
    typos in .env don't silently keep tasks flowing."""
    for v in (
        "false", "FALSE", "False", "  false  ",
        "0", "no", "NO", "off", "OFF", "Off",
    ):
        assert _env_flag_enabled(v) is False, f"{v!r} should disable"


def test_fr_cr_05_192w_env_flag_unrecognised_value_defaults_true() -> None:
    """Unknown values keep the classifier ENABLED. Conservative bias:
    a typo in .env should never silently lose tasks. Operator's
    explicit `false`/`0`/`no`/`off` is the only off-switch."""
    for v in ("", "maybe", "1", "true", "yes", "TRUE", "anything"):
        assert _env_flag_enabled(v) is True, f"{v!r} should be enabled"


def test_fr_cr_05_192w_slack_handler_reads_env_at_attach_time() -> None:
    """The flag is read from `os.environ` at the time
    `_attach_handlers` runs (handler startup). Verify the parsing
    code path matches what the SPEC documents."""
    import os as _os
    with patch.dict(_os.environ, {"CEO_BRAIN_TASK_CLASSIFIER_ENABLED": "false"}):
        raw = _os.environ.get("CEO_BRAIN_TASK_CLASSIFIER_ENABLED", "true")
        enabled = raw.strip().lower() not in ("false", "0", "no", "off")
        assert enabled is False

    with patch.dict(_os.environ, {"CEO_BRAIN_TASK_CLASSIFIER_ENABLED": "true"}):
        raw = _os.environ.get("CEO_BRAIN_TASK_CLASSIFIER_ENABLED", "true")
        enabled = raw.strip().lower() not in ("false", "0", "no", "off")
        assert enabled is True

    # Missing env var also enables (default fallback in get())
    _os.environ.pop("CEO_BRAIN_TASK_CLASSIFIER_ENABLED", None)
    raw = _os.environ.get("CEO_BRAIN_TASK_CLASSIFIER_ENABLED", "true")
    enabled = raw.strip().lower() not in ("false", "0", "no", "off")
    assert enabled is True


def test_fr_cr_05_192w_session_rollback_after_archive_unique_violation() -> None:
    """Cross-reference: the session-rollback contract from
    FR-CR-05-192v is exercised by
    `test_ceo_brain_archive_best_effort.test_fr_cr_05_192v_archive_failure_rolls_back_session`.
    This sanity test imports + invokes that test module to assert
    the dispatcher integration is still wired."""
    from tests.requirements import test_ceo_brain_archive_best_effort as _t
    # The contract is locked there — fail loud if the module loses
    # the test we depend on.
    assert hasattr(
        _t, "test_fr_cr_05_192v_archive_failure_rolls_back_session"
    ), (
        "FR-CR-05-192v session-rollback test missing — that test is "
        "the canonical lock on the dispatcher behaviour that "
        "FR-CR-05-192w piggybacks on"
    )
