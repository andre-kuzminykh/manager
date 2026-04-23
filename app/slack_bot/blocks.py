"""Block Kit payload builders for draft cards and modals."""
from __future__ import annotations

from typing import Any

from app.schemas.intent import IntentClassification, IntentType


# ---- action / block IDs -----------------------------------------------------

ACTION_CONFIRM = "draft_confirm"
ACTION_EDIT = "draft_edit"
ACTION_IGNORE = "draft_ignore"
ACTION_RETRY = "draft_retry"

# CR-01 task card actions
ACTION_START_WORK = "task_start_work"
ACTION_SUBMIT_REVIEW = "task_submit_review"
ACTION_MARK_DONE = "task_mark_done"
ACTION_SUBSCRIBE = "task_subscribe"
ACTION_UNSUBSCRIBE = "task_unsubscribe"
ACTION_OPEN_SOURCE = "task_open_source"
ACTION_SHOW_CONTEXT = "task_show_context"

MODAL_CALLBACK_TASK = "task_modal_submit"
MODAL_CALLBACK_MEETING = "meeting_modal_submit"
MODAL_CALLBACK_CONTEXT = "task_context_view"

BLOCK_TITLE = "title_block"
BLOCK_DESCRIPTION = "description_block"
BLOCK_OWNER = "owner_block"
BLOCK_PRIORITY = "priority_block"
BLOCK_DUE = "due_block"
BLOCK_PARTICIPANTS = "participants_block"
BLOCK_DATETIME = "datetime_block"
BLOCK_NOTES = "notes_block"
BLOCK_EFFORT = "effort_block"

INPUT_TITLE = "title_input"
INPUT_DESCRIPTION = "description_input"
INPUT_OWNER = "owner_input"
INPUT_PRIORITY = "priority_input"
INPUT_DUE = "due_input"
INPUT_PARTICIPANTS = "participants_input"
INPUT_DATETIME = "datetime_input"
INPUT_NOTES = "notes_input"
INPUT_EFFORT = "effort_input"


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
    allowed_owners: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Create-task modal.

    If ``allowed_owners`` is provided, the Owner field renders as a
    constrained static_select (FR-CR-1). Otherwise it falls back to a free
    text input.
    """
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

    if allowed_owners:
        options = [
            {
                "text": {"type": "plain_text", "text": o["display_name"]},
                "value": o["slack_user_id"],
            }
            for o in allowed_owners
        ]
        owner_element: dict[str, Any] = {
            "type": "static_select",
            "action_id": INPUT_OWNER,
            "options": options,
        }
        init_owner_id = initial.get("owner_user_id")
        init_option = next((o for o in options if o["value"] == init_owner_id), None)
        if init_option:
            owner_element["initial_option"] = init_option
    else:
        owner_element = {"type": "plain_text_input", "action_id": INPUT_OWNER}
        if initial.get("owner_display_name"):
            owner_element["initial_value"] = initial["owner_display_name"]

    due_element: dict[str, Any] = {"type": "datepicker", "action_id": INPUT_DUE}
    if initial.get("due_date"):
        due_element["initial_date"] = initial["due_date"]

    effort_element = {
        "type": "plain_text_input",
        "action_id": INPUT_EFFORT,
        "placeholder": {"type": "plain_text", "text": "Estimated minutes (optional)"},
    }
    if initial.get("estimated_minutes") is not None:
        effort_element["initial_value"] = str(initial["estimated_minutes"])

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
            {
                "type": "input",
                "block_id": BLOCK_EFFORT,
                "optional": True,
                "label": {"type": "plain_text", "text": "Estimated effort (min)"},
                "element": effort_element,
            },
        ],
    }


def task_card(
    *,
    task,
    viewer_slack_user_id: str | None = None,
    is_subscribed: bool = False,
    reasoning: str | None = None,
) -> list[dict[str, Any]]:
    """Post-confirmation task card (CR-01)."""
    from app.models import TaskStatus

    title = f"*#{task.id}* {task.title}"
    meta_parts = [f"`{task.status.value}`"]
    if task.owner_display_name or task.owner_user_id:
        meta_parts.append(
            f"owner: <@{task.owner_user_id}>"
            if task.owner_user_id
            else f"owner: {task.owner_display_name}"
        )
    if task.due_date:
        meta_parts.append(f"due: {task.due_date.isoformat()}")
    if task.priority:
        meta_parts.append(f"priority: {task.priority.value}")

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": title},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": " · ".join(meta_parts)}],
        },
    ]
    if task.description:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": task.description}}
        )
    if reasoning:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"_why detected:_ {reasoning}"}
                ],
            }
        )

    elements: list[dict[str, Any]] = []
    is_owner = (
        viewer_slack_user_id is not None
        and task.owner_user_id == viewer_slack_user_id
    )
    if task.status == TaskStatus.todo or task.status == TaskStatus.backlog:
        if is_owner:
            elements.append(
                {
                    "type": "button",
                    "style": "primary",
                    "action_id": ACTION_START_WORK,
                    "text": {"type": "plain_text", "text": "Начать работу"},
                    "value": str(task.id),
                }
            )
    if task.status == TaskStatus.in_progress:
        elements.append(
            {
                "type": "button",
                "action_id": ACTION_SUBMIT_REVIEW,
                "text": {"type": "plain_text", "text": "Submit for review"},
                "value": str(task.id),
            }
        )
        elements.append(
            {
                "type": "button",
                "style": "primary",
                "action_id": ACTION_MARK_DONE,
                "text": {"type": "plain_text", "text": "Mark done"},
                "value": str(task.id),
            }
        )
    if task.status == TaskStatus.review:
        elements.append(
            {
                "type": "button",
                "style": "primary",
                "action_id": ACTION_MARK_DONE,
                "text": {"type": "plain_text", "text": "Mark done"},
                "value": str(task.id),
            }
        )

    # Subscribe toggle (always present except on Done).
    if task.status != TaskStatus.done:
        elements.append(
            {
                "type": "button",
                "action_id": ACTION_UNSUBSCRIBE if is_subscribed else ACTION_SUBSCRIBE,
                "text": {
                    "type": "plain_text",
                    "text": "Отписаться" if is_subscribed else "Подписаться",
                },
                "value": str(task.id),
            }
        )

    # Open source link (URL button).
    if task.source_permalink:
        elements.append(
            {
                "type": "button",
                "action_id": ACTION_OPEN_SOURCE,
                "url": task.source_permalink,
                "text": {"type": "plain_text", "text": "Open source"},
                "value": str(task.id),
            }
        )

    # Show context (opens a modal with the snapshot).
    if task.context_snapshot_id:
        elements.append(
            {
                "type": "button",
                "action_id": ACTION_SHOW_CONTEXT,
                "text": {"type": "plain_text", "text": "Show context"},
                "value": str(task.context_snapshot_id),
            }
        )

    if elements:
        blocks.append({"type": "actions", "block_id": f"task_actions_{task.id}", "elements": elements})
    return blocks


def context_view_modal(*, snapshot) -> dict[str, Any]:
    """Read-only modal showing the context snapshot that produced a task."""
    header = f"Context for source {snapshot.source_ts} in {snapshot.conversation_id}"

    def _line(m: dict[str, Any]) -> str:
        user = m.get("user") or "?"
        text = (m.get("text") or "").strip()
        return f"*<@{user}>* — {text}"

    lines = []
    for m in snapshot.history_before or []:
        lines.append(_line(m))
    lines.append(">>> " + _line(snapshot.source_message or {}))
    for m in snapshot.thread_messages or []:
        if m.get("ts") != (snapshot.source_message or {}).get("ts"):
            lines.append(_line(m))

    body = "\n\n".join(lines) if lines else "(context is empty)"
    return {
        "type": "modal",
        "callback_id": MODAL_CALLBACK_CONTEXT,
        "title": {"type": "plain_text", "text": "Task context"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"_{header}_"}},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": body[:2800]}},
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
