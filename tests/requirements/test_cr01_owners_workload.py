"""Tests for CR-01 FR-CR-1 (allowed owners) + FR-CR-2 (workload estimator)."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.config import Settings
from app.models import Task, TaskStatus
from app.services import WorkloadEstimator, resolve_owner_hint


# =========================================================================== #
# FR-CR-1: Allowed owners config
# =========================================================================== #


def test_fr_cr1_empty_config_returns_empty_list():
    s = Settings(ALLOWED_OWNERS="")
    assert s.allowed_owners() == []


def test_fr_cr1_malformed_json_returns_empty_list():
    s = Settings(ALLOWED_OWNERS="not json")
    assert s.allowed_owners() == []


def test_fr_cr1_parses_valid_json_array():
    s = Settings(
        ALLOWED_OWNERS='[{"slack_user_id":"U1","display_name":"Alice"},'
        '{"slack_user_id":"U2","display_name":"Bob"}]'
    )
    owners = s.allowed_owners()
    assert len(owners) == 2
    assert owners[0]["slack_user_id"] == "U1"
    assert owners[1]["display_name"] == "Bob"


def test_fr_cr1_entries_without_user_id_dropped():
    s = Settings(ALLOWED_OWNERS='[{"display_name":"X"},{"slack_user_id":"U1"}]')
    owners = s.allowed_owners()
    assert len(owners) == 1
    assert owners[0]["slack_user_id"] == "U1"


def test_fr_cr1_missing_display_name_falls_back_to_id():
    s = Settings(ALLOWED_OWNERS='[{"slack_user_id":"U1"}]')
    assert s.allowed_owners()[0]["display_name"] == "U1"


def test_fr_cr1_resolve_hint_mention_takes_precedence():
    allowed = [{"slack_user_id": "U1", "display_name": "Alice"}]
    assert resolve_owner_hint(hint_text="<@U1> please", allowed_owners=allowed)["slack_user_id"] == "U1"


def test_fr_cr1_resolve_hint_exact_name_match():
    allowed = [{"slack_user_id": "U1", "display_name": "Alice"}]
    assert resolve_owner_hint(hint_text="Alice", allowed_owners=allowed)["slack_user_id"] == "U1"


def test_fr_cr1_resolve_hint_substring():
    allowed = [{"slack_user_id": "U2", "display_name": "Bob"}]
    assert resolve_owner_hint(hint_text="Assign to Bob tomorrow", allowed_owners=allowed)["slack_user_id"] == "U2"


def test_fr_cr1_resolve_hint_longest_match_wins():
    allowed = [
        {"slack_user_id": "U1", "display_name": "Ivan"},
        {"slack_user_id": "U2", "display_name": "Ivanov Sr."},
    ]
    result = resolve_owner_hint(hint_text="дай Ivanov Sr.", allowed_owners=allowed)
    assert result["slack_user_id"] == "U2"


def test_fr_cr1_resolve_hint_rejects_unknown_mention():
    allowed = [{"slack_user_id": "U1", "display_name": "Alice"}]
    assert resolve_owner_hint(hint_text="<@UXX>", allowed_owners=allowed) is None


def test_fr_cr1_resolve_hint_none_for_empty_text():
    assert resolve_owner_hint(hint_text="", allowed_owners=[]) is None


def test_fr_cr1_resolve_hint_returns_none_when_no_match():
    allowed = [{"slack_user_id": "U1", "display_name": "Alice"}]
    assert resolve_owner_hint(hint_text="please handle", allowed_owners=allowed) is None


def test_fr_cr1_task_modal_without_allowed_owners_uses_text_input():
    from app.slack_bot import blocks as bk

    view = bk.task_modal(private_metadata="{}")
    owner_block = next(b for b in view["blocks"] if b["block_id"] == bk.BLOCK_OWNER)
    assert owner_block["element"]["type"] == "plain_text_input"


def test_fr_cr1_task_modal_with_allowed_owners_uses_static_select():
    from app.slack_bot import blocks as bk

    allowed = [{"slack_user_id": "U1", "display_name": "Alice"}]
    view = bk.task_modal(private_metadata="{}", allowed_owners=allowed)
    owner_block = next(b for b in view["blocks"] if b["block_id"] == bk.BLOCK_OWNER)
    assert owner_block["element"]["type"] == "static_select"
    assert owner_block["element"]["options"][0]["value"] == "U1"


def test_fr_cr1_task_modal_preselects_initial_owner_when_in_allowed_list():
    from app.slack_bot import blocks as bk

    allowed = [
        {"slack_user_id": "U1", "display_name": "Alice"},
        {"slack_user_id": "U2", "display_name": "Bob"},
    ]
    view = bk.task_modal(
        private_metadata="{}", initial={"owner_user_id": "U2"}, allowed_owners=allowed
    )
    owner_block = next(b for b in view["blocks"] if b["block_id"] == bk.BLOCK_OWNER)
    assert owner_block["element"]["initial_option"]["value"] == "U2"


# =========================================================================== #
# FR-CR-2: Workload estimator
# =========================================================================== #


def _add_open_task(
    session, *, owner="U-owner", minutes=60, status=TaskStatus.todo, due=None
):
    t = Task(
        title="t",
        owner_user_id=owner,
        estimated_minutes=minutes,
        status=status,
        due_date=due,
    )
    session.add(t)
    session.flush()
    return t


def test_fr_cr2_empty_queue_proposes_today_or_next_business_day(session):
    est = WorkloadEstimator()
    today = date(2026, 4, 20)  # Monday
    proposal = est.propose_due_date(
        session, owner_user_id="U-owner", estimated_minutes=60, today=today
    )
    assert proposal.due_date == today


def test_fr_cr2_skips_weekend_to_monday(session):
    est = WorkloadEstimator()
    saturday = date(2026, 4, 25)  # Saturday
    proposal = est.propose_due_date(
        session, owner_user_id="U-owner", estimated_minutes=60, today=saturday
    )
    assert proposal.due_date.weekday() < 5


def test_fr_cr2_considers_existing_workload(session):
    est = WorkloadEstimator(minutes_per_day=60, default_task_minutes=60)
    # 2 existing tasks at 60 min each = 2 days of work
    for _ in range(2):
        _add_open_task(session, owner="U-owner", minutes=60)
    proposal = est.propose_due_date(
        session,
        owner_user_id="U-owner",
        estimated_minutes=60,
        today=date(2026, 4, 20),  # Monday
    )
    # backlog 120 + new 60 = 180; at 60/day → 3 business days → Wed
    assert proposal.due_date == date(2026, 4, 22)  # Wed
    assert proposal.backlog_minutes == 120
    assert proposal.busy_business_days == 3


def test_fr_cr2_done_tasks_not_counted(session):
    est = WorkloadEstimator(minutes_per_day=60)
    _add_open_task(session, status=TaskStatus.done, minutes=600)
    _add_open_task(session, status=TaskStatus.todo, minutes=60)
    assert est.owner_backlog_minutes(session, "U-owner") == 60


@pytest.mark.parametrize(
    "status",
    [TaskStatus.backlog, TaskStatus.todo, TaskStatus.in_progress],
)
def test_fr_cr2_all_open_statuses_count_as_workload(session, status):
    est = WorkloadEstimator()
    _add_open_task(session, status=status, minutes=120)
    assert est.owner_backlog_minutes(session, "U-owner") == 120


def test_fr_cr2_nonexistent_owner_returns_zero_backlog(session):
    est = WorkloadEstimator()
    assert est.owner_backlog_minutes(session, "U-unknown") == 0


def test_fr_cr2_none_owner_returns_zero_backlog(session):
    est = WorkloadEstimator()
    assert est.owner_backlog_minutes(session, "") == 0


def test_fr_cr2_uses_default_minutes_for_tasks_without_estimate(session):
    est = WorkloadEstimator(default_task_minutes=90)
    _add_open_task(session, minutes=None)
    assert est.owner_backlog_minutes(session, "U-owner") == 90


def test_fr_cr2_proposal_contains_report_fields(session):
    est = WorkloadEstimator(minutes_per_day=360)
    proposal = est.propose_due_date(
        session,
        owner_user_id="U-owner",
        estimated_minutes=120,
        today=date(2026, 4, 20),
    )
    assert proposal.new_task_minutes == 120
    assert proposal.backlog_minutes == 0
    assert proposal.busy_business_days >= 1
