"""Block Kit payload builders for draft cards and modals."""
from __future__ import annotations

from typing import Any

from app.schemas.intent import IntentClassification, IntentType


# ---- action / block IDs -----------------------------------------------------

ACTION_CONFIRM = "draft_confirm"
ACTION_EDIT = "draft_edit"
ACTION_IGNORE = "draft_ignore"
ACTION_RETRY = "draft_retry"

MODAL_CALLBACK_TASK = "task_modal_submit"
MODAL_CALLBACK_MEETING = "meeting_modal_submit"

BLOCK_TITLE = "title_block"
BLOCK_DESCRIPTION = "description_block"
BLOCK_OWNER = "owner_block"
BLOCK_PRIORITY = "priority_block"
BLOCK_DUE = "due_block"
BLOCK_PARTICIPANTS = "participants_block"
BLOCK_DATETIME = "datetime_block"
BLOCK_NOTES = "notes_block"

INPUT_TITLE = "title_input"
INPUT_DESCRIPTION = "description_input"
INPUT_OWNER = "owner_input"
INPUT_PRIORITY = "priority_input"
INPUT_DUE = "due_input"
INPUT_PARTICIPANTS = "participants_input"
INPUT_DATETIME = "datetime_input"
INPUT_NOTES = "notes_input"


# ---- cards ------------------------------------------------------------------


def _fmt(v: Any, default: str = "—") -> str:
    if v is None or v == "":
        return default
    if isinstance(v, list):
        return ", ".join(v) if v else default
    return str(v)


def draft_card(
    *,
    classification: IntentClassification,
    draft_id: int,
    confidence_bucket: str,
) -> list[dict[str, Any]]:
    """Confirmation card with Confirm / Edit / Ignore buttons."""
    header = {
        "task": "Task draft",
        "meeting": "Meeting draft",
    }

    if classification.intent in (IntentType.create_task, IntentType.update_task):
        kind = "task"
        d = classification.task
        fields = [
            ("Title", _fmt(d.title if d else None)),
            ("Owner", _fmt(d.owner_display_name if d else None)),
            ("Priority", _fmt(d.priority if d else None)),
            ("Due", _fmt(d.due_date.isoformat() if d and d.due_date else None)),
        ]
    elif classification.intent in (IntentType.create_meeting, IntentType.update_meeting):
        kind = "meeting"
        m = classification.meeting
        fields = [
            ("Title", _fmt(m.title if m else None)),
            ("When", _fmt(m.datetime_at.isoformat() if m and m.datetime_at else None)),
            ("Participants", _fmt(m.participants if m else None)),
            ("Notes", _fmt(m.notes if m else None)),
        ]
    else:
        kind = "task"
        fields = [("Intent", "no_action")]

    confidence_label = {
        "high": "high confidence",
        "medium": "medium confidence",
        "low": "low confidence",
    }.get(confidence_bucket, confidence_bucket)

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": header.get(kind, "Draft"), "emoji": True},
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"*{classification.intent.value}* · {confidence_label} "
                        f"({classification.confidence:.2f})"
                    ),
                }
            ],
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*{label}*\n{value}"} for label, value in fields
            ],
        },
        {
            "type": "actions",
            "block_id": f"draft_actions_{draft_id}",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "action_id": ACTION_CONFIRM,
                    "text": {"type": "plain_text", "text": "Confirm"},
                    "value": str(draft_id),
                },
                {
                    "type": "button",
                    "action_id": ACTION_EDIT,
                    "text": {"type": "plain_text", "text": "Edit"},
                    "value": str(draft_id),
                },
                {
                    "type": "button",
                    "style": "danger",
                    "action_id": ACTION_IGNORE,
                    "text": {"type": "plain_text", "text": "Ignore"},
                    "value": str(draft_id),
                },
            ],
        },
    ]
    return blocks


def soft_prompt(intent: IntentType, draft_id: int) -> list[dict[str, Any]]:
    """Soft prompt shown on medium-confidence detection."""
    label = {
        IntentType.create_task: "Похоже, это задача. Создать?",
        IntentType.create_meeting: "Похоже, это встреча. Создать?",
        IntentType.update_task: "Похоже, это обновление задачи. Применить?",
        IntentType.update_meeting: "Похоже, это обновление встречи. Применить?",
    }.get(intent, "Создать действие?")

    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": label}},
        {
            "type": "actions",
            "block_id": f"soft_actions_{draft_id}",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "action_id": ACTION_CONFIRM,
                    "text": {"type": "plain_text", "text": "Yes"},
                    "value": str(draft_id),
                },
                {
                    "type": "button",
                    "action_id": ACTION_IGNORE,
                    "text": {"type": "plain_text", "text": "No"},
                    "value": str(draft_id),
                },
            ],
        },
    ]


# ---- modals -----------------------------------------------------------------


_PRIORITY_OPTIONS = [
    {"text": {"type": "plain_text", "text": label}, "value": value}
    for label, value in [
        ("Low", "low"),
        ("Medium", "medium"),
        ("High", "high"),
        ("Urgent", "urgent"),
    ]
]


def task_modal(
    *,
    private_metadata: str,
    initial: dict[str, Any] | None = None,
) -> dict[str, Any]:
    initial = initial or {}
    priority = initial.get("priority", "medium")
    priority_option = next(
        (o for o in _PRIORITY_OPTIONS if o["value"] == priority), _PRIORITY_OPTIONS[1]
    )

    title_element = {
        "type": "plain_text_input",
        "action_id": INPUT_TITLE,
        "placeholder": {"type": "plain_text", "text": "e.g. Prepare list of funds"},
    }
    if initial.get("title"):
        title_element["initial_value"] = initial["title"]

    description_element = {
        "type": "plain_text_input",
        "action_id": INPUT_DESCRIPTION,
        "multiline": True,
    }
    if initial.get("description"):
        description_element["initial_value"] = initial["description"]

    owner_element = {"type": "plain_text_input", "action_id": INPUT_OWNER}
    if initial.get("owner_display_name"):
        owner_element["initial_value"] = initial["owner_display_name"]

    due_element: dict[str, Any] = {"type": "datepicker", "action_id": INPUT_DUE}
    if initial.get("due_date"):
        due_element["initial_date"] = initial["due_date"]

    return {
        "type": "modal",
        "callback_id": MODAL_CALLBACK_TASK,
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "Create task"},
        "submit": {"type": "plain_text", "text": "Create"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": BLOCK_TITLE,
                "label": {"type": "plain_text", "text": "Title"},
                "element": title_element,
            },
            {
                "type": "input",
                "block_id": BLOCK_DESCRIPTION,
                "optional": True,
                "label": {"type": "plain_text", "text": "Description"},
                "element": description_element,
            },
            {
                "type": "input",
                "block_id": BLOCK_OWNER,
                "optional": True,
                "label": {"type": "plain_text", "text": "Owner"},
                "element": owner_element,
            },
            {
                "type": "input",
                "block_id": BLOCK_PRIORITY,
                "label": {"type": "plain_text", "text": "Priority"},
                "element": {
                    "type": "static_select",
                    "action_id": INPUT_PRIORITY,
                    "options": _PRIORITY_OPTIONS,
                    "initial_option": priority_option,
                },
            },
            {
                "type": "input",
                "block_id": BLOCK_DUE,
                "optional": True,
                "label": {"type": "plain_text", "text": "Due date"},
                "element": due_element,
            },
        ],
    }


def meeting_modal(
    *,
    private_metadata: str,
    initial: dict[str, Any] | None = None,
) -> dict[str, Any]:
    initial = initial or {}

    title_element = {"type": "plain_text_input", "action_id": INPUT_TITLE}
    if initial.get("title"):
        title_element["initial_value"] = initial["title"]

    participants_element = {
        "type": "plain_text_input",
        "action_id": INPUT_PARTICIPANTS,
        "placeholder": {"type": "plain_text", "text": "Comma-separated names or @handles"},
    }
    if initial.get("participants"):
        participants_element["initial_value"] = ", ".join(initial["participants"])

    datetime_element: dict[str, Any] = {
        "type": "datetimepicker",
        "action_id": INPUT_DATETIME,
    }
    if initial.get("datetime_ts"):
        datetime_element["initial_date_time"] = int(initial["datetime_ts"])

    notes_element = {
        "type": "plain_text_input",
        "action_id": INPUT_NOTES,
        "multiline": True,
    }
    if initial.get("notes"):
        notes_element["initial_value"] = initial["notes"]

    return {
        "type": "modal",
        "callback_id": MODAL_CALLBACK_MEETING,
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "Create meeting"},
        "submit": {"type": "plain_text", "text": "Create"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": BLOCK_TITLE,
                "label": {"type": "plain_text", "text": "Title"},
                "element": title_element,
            },
            {
                "type": "input",
                "block_id": BLOCK_PARTICIPANTS,
                "optional": True,
                "label": {"type": "plain_text", "text": "Participants"},
                "element": participants_element,
            },
            {
                "type": "input",
                "block_id": BLOCK_DATETIME,
                "label": {"type": "plain_text", "text": "When"},
                "element": datetime_element,
            },
            {
                "type": "input",
                "block_id": BLOCK_NOTES,
                "optional": True,
                "label": {"type": "plain_text", "text": "Notes"},
                "element": notes_element,
            },
        ],
    }


def success_message(entity_type: str, entity_id: int, summary: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":white_check_mark: {entity_type.capitalize()} #{entity_id} created: *{summary}*",
            },
        }
    ]


def failure_message(entity_type: str, error: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":warning: Failed to create {entity_type}: `{error}`",
            },
        }
    ]
