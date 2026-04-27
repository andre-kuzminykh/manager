"""Requirement coverage: FR-CR-04-18 (modal cleanup + colored
priority emoji).

The Edit/Create modal now omits the standalone "Recurring" checkbox
and the "Estimated effort (min)" block; selecting any weekday in the
recurring multi-select acts as the toggle. Priority labels carry
coloured circle emoji both in the static_select options and on the
task card meta line."""
from __future__ import annotations

from datetime import date

from app.models import Task
from app.models.task import TaskPriority, TaskStatus
from app.slack_bot import blocks as bk


def _modal_block_ids(view: dict) -> list[str]:
    return [b.get("block_id") for b in view["blocks"]]


# --------------------------------------------------------------------------- #
# Modal: removed blocks
# --------------------------------------------------------------------------- #


def test_task_modal_omits_standalone_recurring_checkbox():
    view = bk.task_modal(private_metadata="{}")
    assert bk.BLOCK_RECURRING not in _modal_block_ids(view)


def test_task_modal_omits_estimated_effort_block():
    view = bk.task_modal(private_metadata="{}")
    assert bk.BLOCK_EFFORT not in _modal_block_ids(view)


def test_task_modal_keeps_recurring_weekdays_block():
    view = bk.task_modal(private_metadata="{}")
    assert bk.BLOCK_RECURRING_WEEKDAYS in _modal_block_ids(view)


# --------------------------------------------------------------------------- #
# Priority emoji on the modal options
# --------------------------------------------------------------------------- #


def test_task_modal_priority_options_carry_emoji_prefix():
    view = bk.task_modal(private_metadata="{}")
    priority_block = next(
        b for b in view["blocks"] if b.get("block_id") == bk.BLOCK_PRIORITY
    )
    options = priority_block["element"]["options"]
    by_value = {o["value"]: o["text"]["text"] for o in options}
    assert ":large_green_circle:" in by_value["low"]
    assert ":large_yellow_circle:" in by_value["medium"]
    assert ":large_orange_circle:" in by_value["high"]
    assert ":red_circle:" in by_value["urgent"]


# --------------------------------------------------------------------------- #
# Card meta line uses the same emoji
# --------------------------------------------------------------------------- #


def test_task_card_priority_meta_includes_color_emoji():
    task = Task(
        id=1,
        title="t",
        status=TaskStatus.todo,
        owner_user_id="U-owner",
        priority=TaskPriority.high,
        due_date=date(2026, 5, 1),
    )
    flat = str(bk.task_card(task=task, viewer_slack_user_id="U-owner"))
    assert ":large_orange_circle: high" in flat


def test_priority_emoji_dict_covers_every_priority_value():
    # Catch the case where someone adds a new TaskPriority but forgets
    # to register a colour for it.
    for p in TaskPriority:
        assert p.value in bk.PRIORITY_EMOJI


# --------------------------------------------------------------------------- #
# Submit semantics: weekdays alone make a task recurring
# --------------------------------------------------------------------------- #


def test_extract_payload_marks_recurring_when_only_weekdays_selected():
    from app.slack_bot.handlers.views import _extract_task_payload

    view = {
        "state": {
            "values": {
                bk.BLOCK_TITLE: {bk.INPUT_TITLE: {"value": "t"}},
                bk.BLOCK_PRIORITY: {
                    bk.INPUT_PRIORITY: {"selected_option": {"value": "medium"}}
                },
                bk.BLOCK_RECURRING_WEEKDAYS: {
                    bk.INPUT_RECURRING_WEEKDAYS: {
                        "selected_options": [
                            {"value": "mon", "text": {"type": "plain_text", "text": "Mon"}},
                            {"value": "wed", "text": {"type": "plain_text", "text": "Wed"}},
                        ]
                    }
                },
            }
        }
    }
    payload = _extract_task_payload(view)
    assert payload["is_recurring"] is True
    assert payload["recurring_weekdays"] == ["mon", "wed"]


def test_extract_payload_not_recurring_when_no_weekday():
    from app.slack_bot.handlers.views import _extract_task_payload

    view = {
        "state": {
            "values": {
                bk.BLOCK_TITLE: {bk.INPUT_TITLE: {"value": "t"}},
                bk.BLOCK_PRIORITY: {
                    bk.INPUT_PRIORITY: {"selected_option": {"value": "medium"}}
                },
                bk.BLOCK_RECURRING_WEEKDAYS: {
                    bk.INPUT_RECURRING_WEEKDAYS: {"selected_options": []}
                },
            }
        }
    }
    payload = _extract_task_payload(view)
    assert payload["is_recurring"] is False
    assert payload["recurring_weekdays"] == []
