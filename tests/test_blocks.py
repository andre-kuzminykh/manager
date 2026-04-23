from datetime import date

from app.schemas.intent import IntentClassification, IntentType, TaskDraft
from app.slack_bot import blocks as bk


def test_task_draft_card_contains_fields_and_actions():
    c = IntentClassification(
        intent=IntentType.create_task,
        confidence=0.85,
        task=TaskDraft(
            title="Prepare list of funds",
            owner_display_name="@ivan",
            priority="high",
            due_date=date(2026, 5, 2),
        ),
    )
    card = bk.draft_card(classification=c, draft_id=11, confidence_bucket="high")

    action_ids = [
        el["action_id"]
        for block in card
        if block["type"] == "actions"
        for el in block["elements"]
    ]
    assert bk.ACTION_CONFIRM in action_ids
    assert bk.ACTION_EDIT in action_ids
    assert bk.ACTION_IGNORE in action_ids

    fields_block = next(b for b in card if b["type"] == "section" and "fields" in b)
    rendered = "\n".join(f["text"] for f in fields_block["fields"])
    assert "Prepare list of funds" in rendered
    assert "@ivan" in rendered
    assert "high" in rendered
    assert "2026-05-02" in rendered


def test_task_modal_has_required_title_input():
    view = bk.task_modal(private_metadata='{"k":"v"}', initial={"title": "Draft"})
    assert view["type"] == "modal"
    assert view["callback_id"] == bk.MODAL_CALLBACK_TASK
    title_block = next(b for b in view["blocks"] if b["block_id"] == bk.BLOCK_TITLE)
    assert title_block["element"]["initial_value"] == "Draft"
    assert title_block.get("optional") in (False, None)  # required


def test_meeting_modal_includes_datetime_picker():
    view = bk.meeting_modal(private_metadata="{}")
    dt_block = next(b for b in view["blocks"] if b["block_id"] == bk.BLOCK_DATETIME)
    assert dt_block["element"]["type"] == "datetimepicker"


def test_soft_prompt_has_only_yes_no():
    payload = bk.soft_prompt(IntentType.create_task, draft_id=1)
    actions = next(b for b in payload if b["type"] == "actions")
    ids = [el["action_id"] for el in actions["elements"]]
    assert ids == [bk.ACTION_CONFIRM, bk.ACTION_IGNORE]
