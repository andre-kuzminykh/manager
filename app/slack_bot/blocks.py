"""Block Kit payload builders for draft cards and modals."""
from __future__ import annotations

from typing import Any

from app.schemas.intent import IntentClassification, IntentType


# ---- action / block IDs -----------------------------------------------------

ACTION_CONFIRM = "draft_confirm"
ACTION_EDIT = "draft_edit"
ACTION_IGNORE = "draft_ignore"
ACTION_RETRY = "draft_retry"

# Task card actions
ACTION_START_WORK = "task_start_work"
ACTION_MARK_DONE = "task_mark_done"
ACTION_EDIT_TASK = "task_edit"
ACTION_CANCEL_TASK = "task_cancel"
ACTION_DELETE_TASK = "task_delete"
ACTION_DELETE_CONFIRM = "task_delete_confirm"
ACTION_SUBSCRIBE = "task_subscribe"
ACTION_UNSUBSCRIBE = "task_unsubscribe"
ACTION_OPEN_SOURCE = "task_open_source"
ACTION_SHOW_CONTEXT = "task_show_context"

# Daily digest + subscriptions modal
ACTION_MANAGE_SUBSCRIPTIONS = "manage_subscriptions"
ACTION_UNSUBSCRIBE_IN_MODAL = "subs_modal_unsubscribe"

# CR-03 admin review
ACTION_ADMIN_CONFIRM_TASK = "admin_confirm_task"
ACTION_ADMIN_EDIT_TASK = "admin_edit_task"
ACTION_ADMIN_REJECT_TASK = "admin_reject_task"

MODAL_CALLBACK_TASK = "task_modal_submit"
MODAL_CALLBACK_MEETING = "meeting_modal_submit"
MODAL_CALLBACK_CONTEXT = "task_context_view"
MODAL_CALLBACK_SUBSCRIPTIONS = "subscriptions_modal"
MODAL_CALLBACK_ADMIN_EDIT = "admin_edit_task_modal"
MODAL_CALLBACK_EDIT_TASK = "edit_task_modal"
MODAL_CALLBACK_COMPLETE_TASK = "complete_task_modal"
MODAL_CALLBACK_DELETE_TASK = "delete_task_modal"

BLOCK_ARTIFACT = "artifact_block"
INPUT_ARTIFACT_URL = "artifact_url_input"
INPUT_ARTIFACT_TEXT = "artifact_text_input"
BLOCK_ARTIFACT_TEXT = "artifact_text_block"

# CR-03 weekly plan
ACTION_WEEKLY_ACCEPT = "weekly_plan_accept"
ACTION_WEEKLY_DEFER = "weekly_plan_defer"

# CR-04 daily plan (evening approval / morning execution)
ACTION_PLAN_SKIP = "plan_skip_task"
ACTION_PLAN_APPROVE = "plan_approve"

BLOCK_TITLE = "title_block"
BLOCK_DESCRIPTION = "description_block"
BLOCK_OWNER = "owner_block"
BLOCK_PRIORITY = "priority_block"
BLOCK_DUE = "due_block"
BLOCK_DUE_TIME = "due_time_block"
BLOCK_START_DATE = "start_date_block"
BLOCK_START_TIME = "start_time_block"
BLOCK_CATEGORY = "category_block"
BLOCK_RECURRING = "recurring_block"
BLOCK_RECURRING_WEEKDAYS = "recurring_weekdays_block"
BLOCK_RECURRING_START = "recurring_start_block"
BLOCK_RECURRING_END = "recurring_end_block"
BLOCK_PARTICIPANTS = "participants_block"
BLOCK_DATETIME = "datetime_block"
BLOCK_NOTES = "notes_block"
BLOCK_EFFORT = "effort_block"

INPUT_TITLE = "title_input"
INPUT_DESCRIPTION = "description_input"
INPUT_OWNER = "owner_input"
INPUT_PRIORITY = "priority_input"
INPUT_DUE = "due_input"
INPUT_DUE_TIME = "due_time_input"
INPUT_START_DATE = "start_date_input"
INPUT_START_TIME = "start_time_input"
INPUT_CATEGORY = "category_input"
INPUT_RECURRING = "recurring_input"
INPUT_RECURRING_WEEKDAYS = "recurring_weekdays_input"
INPUT_RECURRING_START = "recurring_start_input"
INPUT_RECURRING_END = "recurring_end_input"

# Order matters — same labels are used in the modal UI.
RECURRING_WEEKDAY_OPTIONS = [
    {"value": "mon", "text": "Mon"},
    {"value": "tue", "text": "Tue"},
    {"value": "wed", "text": "Wed"},
    {"value": "thu", "text": "Thu"},
    {"value": "fri", "text": "Fri"},
    {"value": "sat", "text": "Sat"},
    {"value": "sun", "text": "Sun"},
]
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
    missing_fields: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Confirmation card with Confirm / Edit / Ignore buttons.

    ``missing_fields`` is a list of human-readable field names to render as an
    inline prompt ("Please fill in: …"). Pass None or empty list to skip.
    """
    header = {
        "task": "Task draft",
        "meeting": "Meeting draft",
    }

    if classification.intent in (IntentType.create_task, IntentType.update_task):
        kind = "task"
        d = classification.task
        if d is None:
            owner_text = "—"
        elif d.owner_user_id:
            owner_text = f"<@{d.owner_user_id}>"
            if d.owner_assumed:
                owner_text += " _(implicit)_"
        elif d.owner_display_name:
            owner_text = d.owner_display_name
        else:
            owner_text = "—"
        fields = [
            ("Title", _fmt(d.title if d else None)),
            ("Owner", owner_text),
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
    ]

    if missing_fields:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f":pencil2: *Missing:* {', '.join(missing_fields)}. "
                            "Click *Edit* to fill it in, or *Accept* — "
                            "the bot will create the task and ask in the thread."
                        ),
                    }
                ],
            }
        )
    else:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": ":white_check_mark: All filled. *Accept* and the task ships to the tracker.",
                    }
                ],
            }
        )

    blocks.append(
        {
            "type": "actions",
            "block_id": f"draft_actions_{draft_id}",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "action_id": ACTION_CONFIRM,
                    "text": {"type": "plain_text", "text": "Accept"},
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
                    "text": {"type": "plain_text", "text": "Reject"},
                    "value": str(draft_id),
                },
            ],
        }
    )
    return blocks


def soft_prompt(intent: IntentType, draft_id: int) -> list[dict[str, Any]]:
    """Soft prompt shown on medium-confidence detection."""
    label = {
        IntentType.create_task: "Looks like a task. Create?",
        IntentType.create_meeting: "Looks like a meeting. Create?",
        IntentType.update_task: "Looks like a task update. Apply?",
        IntentType.update_meeting: "Looks like a meeting update. Apply?",
    }.get(intent, "Create action?")

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


PRIORITY_EMOJI = {
    "low": ":large_green_circle:",
    "medium": ":large_yellow_circle:",
    "high": ":large_orange_circle:",
    "urgent": ":red_circle:",
}

_PRIORITY_OPTIONS = [
    {
        "text": {
            "type": "plain_text",
            "text": f"{PRIORITY_EMOJI[value]} {label}",
            "emoji": True,
        },
        "value": value,
    }
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

    due_time_element: dict[str, Any] = {
        "type": "timepicker",
        "action_id": INPUT_DUE_TIME,
    }
    if initial.get("due_time"):
        due_time_element["initial_time"] = initial["due_time"]

    start_date_element: dict[str, Any] = {
        "type": "datepicker",
        "action_id": INPUT_START_DATE,
    }
    if initial.get("start_date"):
        start_date_element["initial_date"] = initial["start_date"]

    start_time_element: dict[str, Any] = {
        "type": "timepicker",
        "action_id": INPUT_START_TIME,
    }
    if initial.get("start_time"):
        start_time_element["initial_time"] = initial["start_time"]

    category_element: dict[str, Any] = {
        "type": "plain_text_input",
        "action_id": INPUT_CATEGORY,
        "placeholder": {
            "type": "plain_text",
            "text": "e.g. marketing / dev / ops (optional)",
        },
    }
    if initial.get("category"):
        category_element["initial_value"] = initial["category"]

    # Recurring controls. Always rendered; the checkbox tells us
    # whether to honor the rest of the values on submit.
    weekday_options = [
        {
            "value": o["value"],
            "text": {"type": "plain_text", "text": o["text"]},
        }
        for o in RECURRING_WEEKDAY_OPTIONS
    ]
    weekdays_element: dict[str, Any] = {
        "type": "checkboxes",
        "action_id": INPUT_RECURRING_WEEKDAYS,
        "options": weekday_options,
    }
    initial_wd = initial.get("recurring_weekdays") or []
    if initial_wd:
        weekdays_element["initial_options"] = [
            o for o in weekday_options if o["value"] in initial_wd
        ]

    rec_start_element: dict[str, Any] = {
        "type": "timepicker",
        "action_id": INPUT_RECURRING_START,
    }
    if initial.get("recurring_start_time"):
        rec_start_element["initial_time"] = initial["recurring_start_time"]

    rec_end_element: dict[str, Any] = {
        "type": "timepicker",
        "action_id": INPUT_RECURRING_END,
    }
    if initial.get("recurring_end_time"):
        rec_end_element["initial_time"] = initial["recurring_end_time"]

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
                "block_id": BLOCK_DUE_TIME,
                "optional": True,
                "label": {"type": "plain_text", "text": "Due time (optional)"},
                "element": due_time_element,
            },
            {
                "type": "input",
                "block_id": BLOCK_START_DATE,
                "optional": True,
                "label": {"type": "plain_text", "text": "Start date (optional)"},
                "element": start_date_element,
            },
            {
                "type": "input",
                "block_id": BLOCK_START_TIME,
                "optional": True,
                "label": {"type": "plain_text", "text": "Start time (optional)"},
                "element": start_time_element,
            },
            {
                "type": "input",
                "block_id": BLOCK_CATEGORY,
                "optional": True,
                "label": {"type": "plain_text", "text": "Category (optional)"},
                "element": category_element,
            },
            {
                "type": "input",
                "block_id": BLOCK_RECURRING_WEEKDAYS,
                "optional": True,
                "label": {
                    "type": "plain_text",
                    "text": "Repeat weekdays (optional)",
                },
                "element": weekdays_element,
            },
            {
                "type": "input",
                "block_id": BLOCK_RECURRING_START,
                "optional": True,
                "label": {
                    "type": "plain_text",
                    "text": "Recurring start time",
                },
                "element": rec_start_element,
            },
            {
                "type": "input",
                "block_id": BLOCK_RECURRING_END,
                "optional": True,
                "label": {
                    "type": "plain_text",
                    "text": "Recurring end time",
                },
                "element": rec_end_element,
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

    star = ":star: " if is_subscribed else ""
    title = f"{star}*#{task.id}* {task.title}"
    meta_parts = [f"`{task.status.value}`"]
    if is_subscribed:
        meta_parts.append("subscribed")
    if task.owner_display_name or task.owner_user_id:
        assumed_suffix = (
            " _(implicit)_"
            if (task.extra or {}).get("owner_assumed")
            else ""
        )
        meta_parts.append(
            (
                f"owner: <@{task.owner_user_id}>"
                if task.owner_user_id
                else f"owner: {task.owner_display_name}"
            )
            + assumed_suffix
        )
    if task.due_date:
        due_str = task.due_date.isoformat()
        if getattr(task, "due_time", None):
            due_str += f" {task.due_time.strftime('%H:%M')}"
        meta_parts.append(f"due: {due_str}")
    if getattr(task, "start_date", None):
        start_str = task.start_date.isoformat()
        if getattr(task, "start_time", None):
            start_str += f" {task.start_time.strftime('%H:%M')}"
        meta_parts.append(f"start: {start_str}")
    if getattr(task, "category", None):
        meta_parts.append(f"category: {task.category}")
    if getattr(task, "is_recurring", False) and task.recurring_weekdays:
        wd_label_by_value = {o["value"]: o["text"] for o in RECURRING_WEEKDAY_OPTIONS}
        wd_str = "/".join(
            wd_label_by_value.get(w, w) for w in task.recurring_weekdays
        )
        rec = f":repeat: {wd_str}"
        if task.recurring_start_time and task.recurring_end_time:
            rec += (
                f" {task.recurring_start_time.strftime('%H:%M')}–"
                f"{task.recurring_end_time.strftime('%H:%M')}"
            )
        elif task.recurring_start_time:
            rec += f" from {task.recurring_start_time.strftime('%H:%M')}"
        meta_parts.append(rec)
    if task.priority:
        emoji = PRIORITY_EMOJI.get(task.priority.value, "")
        meta_parts.append(f"priority: {emoji} {task.priority.value}".strip())

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
    # CR-03 FR-CR-03-7: render the completion artifact for done tasks.
    if task.status == TaskStatus.done and task.completion_artifact:
        if task.completion_artifact_kind == "url":
            artifact_text = f":paperclip: <{task.completion_artifact}|Artifact>"
        else:
            artifact_text = f":paperclip: *Artifact:* {task.completion_artifact}"
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": artifact_text}}
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
    from app.services.employees import is_admin as _is_admin

    viewer_is_admin = _is_admin(viewer_slack_user_id)
    # "Start" shows for the owner, OR when the task has no owner assigned
    # yet — in that case any team member can claim it.
    may_start = is_owner or task.owner_user_id is None
    if task.status == TaskStatus.todo or task.status == TaskStatus.backlog:
        if may_start:
            elements.append(
                {
                    "type": "button",
                    "style": "primary",
                    "action_id": ACTION_START_WORK,
                    "text": {"type": "plain_text", "text": "Start"},
                    "value": str(task.id),
                }
            )
    if task.status == TaskStatus.in_progress:
        elements.append(
            {
                "type": "button",
                "style": "primary",
                "action_id": ACTION_MARK_DONE,
                "text": {"type": "plain_text", "text": "Mark done"},
                "value": str(task.id),
            }
        )

    # Edit is available to the owner and to admins (not to bystanders).
    if task.status != TaskStatus.done and (is_owner or viewer_is_admin):
        elements.append(
            {
                "type": "button",
                "action_id": ACTION_EDIT_TASK,
                "text": {"type": "plain_text", "text": "Edit"},
                "value": str(task.id),
            }
        )

    # Cancel — drops in_progress / todo / done back to todo (this week)
    # or backlog (later), without requiring an artifact. Owner + admin.
    if task.status != TaskStatus.backlog and (is_owner or viewer_is_admin):
        elements.append(
            {
                "type": "button",
                "action_id": ACTION_CANCEL_TASK,
                "text": {"type": "plain_text", "text": "Cancel"},
                "value": str(task.id),
            }
        )

    # Subscribe toggle. Owner never sees it — they are implicitly subscribed
    # by virtue of being the assignee, so the button would be redundant.
    if task.status != TaskStatus.done and not is_owner:
        elements.append(
            {
                "type": "button",
                "action_id": ACTION_UNSUBSCRIBE if is_subscribed else ACTION_SUBSCRIBE,
                "text": {
                    "type": "plain_text",
                    "text": "Unsubscribe" if is_subscribed else "Subscribe",
                },
                "value": str(task.id),
            }
        )

    # "Open source" button intentionally omitted — the card lives in the
    # source thread, so the source is already one scroll away. The
    # permalink is still rendered inside success_message() for DMs.

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

    # Delete — destructive, owner + admin only, opens a confirmation modal.
    if is_owner or viewer_is_admin:
        elements.append(
            {
                "type": "button",
                "style": "danger",
                "action_id": ACTION_DELETE_TASK,
                "text": {"type": "plain_text", "text": "Delete"},
                "value": str(task.id),
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


def success_message(
    entity_type: str,
    entity_id: int,
    summary: str,
    *,
    permalink: str | None = None,
) -> list[dict[str, Any]]:
    text = f":white_check_mark: {entity_type.capitalize()} *#{entity_id}* created: *{summary}*"
    if permalink:
        text += f"\n<{permalink}|Open source message>"
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


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


# ---- Daily digest + subscriptions modal -----------------------------------


def _tasks_mrkdwn(tasks: list[Any], *, include_owner: bool = False) -> str:
    if not tasks:
        return "(empty)"
    lines = []
    for t in tasks:
        line = f"• *#{t.id}* {t.title} · `{t.status.value}`"
        if t.due_date:
            line += f" · due {t.due_date.isoformat()}"
        if include_owner and t.owner_user_id:
            line += f" · owner <@{t.owner_user_id}>"
        lines.append(line)
    return "\n".join(lines)


def _today_tasks_mrkdwn(tasks: list[Any]) -> str:
    """FR-CR-05-01 morning digest «Today» renderer — minimal copy.

    Drops `#id`, owner, status, repeated due-date (every task here
    is by definition due today). Keeps title + optional description
    + priority + optional category + optional start / due times.
    """
    if not tasks:
        return "(empty)"
    lines: list[str] = []
    for t in tasks:
        head = f"• *{t.title}*"
        meta: list[str] = [t.priority.value]
        if t.category:
            meta.append(t.category)
        if getattr(t, "start_time", None):
            meta.append(f"start {t.start_time.strftime('%H:%M')}")
        if getattr(t, "due_time", None):
            meta.append(f"due {t.due_time.strftime('%H:%M')}")
        block = head
        if t.description:
            block += f"\n  _{t.description}_"
        block += f"\n  {' · '.join(meta)}"
        lines.append(block)
    return "\n".join(lines)


def daily_digest_blocks(
    *,
    today,
    today_tasks: list[Any],
    approaching: list[Any] | None = None,  # legacy — ignored under FR-CR-05-01
    overdue: list[Any] | None = None,      # legacy — ignored under FR-CR-05-01
    tracked: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """FR-CR-05-01 — morning digest narrows to «Today's tasks» only.

    The previous *Today / Approaching / Overdue* mash-up moved to
    dedicated reminders (deadline reminder fires per task as it
    approaches; overdue gets its own line in the evening 3-section
    DM under FR-CR-05-04). The Slack callers still pass the old
    arguments for back-compat — we silently drop them.
    """
    tracked = tracked or []
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Your tasks for {today.isoformat()}",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Today ({len(today_tasks)})*\n{_today_tasks_mrkdwn(today_tasks)}",
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Tracking ({len(tracked)})*\n"
                    + _tasks_mrkdwn(tracked, include_owner=True)
                ),
            },
        },
        {
            "type": "actions",
            "block_id": "digest_actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": ACTION_MANAGE_SUBSCRIPTIONS,
                    "text": {
                        "type": "plain_text",
                        "text": "Manage subscriptions",
                    },
                    "value": "manage",
                }
            ],
        },
    ]
    return blocks


def complete_task_modal(*, task_id: int) -> dict[str, Any]:
    """Modal shown when the assignee clicks *Mark done* — both fields
    (URL + text note) are optional. The submit handler accepts an empty
    form: completing without an artifact is a valid choice (FR-CR-04-21).
    """
    return {
        "type": "modal",
        "callback_id": MODAL_CALLBACK_COMPLETE_TASK,
        "private_metadata": str(task_id),
        "title": {"type": "plain_text", "text": "Complete task"},
        "submit": {"type": "plain_text", "text": "Complete"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "Optional — attach a link or a short note about the result.",
                },
            },
            {
                "type": "input",
                "block_id": BLOCK_ARTIFACT,
                "optional": True,
                "label": {"type": "plain_text", "text": "Artifact URL"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": INPUT_ARTIFACT_URL,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "https://...",
                    },
                },
            },
            {
                "type": "input",
                "block_id": BLOCK_ARTIFACT_TEXT,
                "optional": True,
                "label": {"type": "plain_text", "text": "Or description"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": INPUT_ARTIFACT_TEXT,
                    "multiline": True,
                },
            },
        ],
    }


def delete_task_modal(*, task_id: int, title: str) -> dict[str, Any]:
    """Confirmation modal shown when the owner / admin clicks *Delete*.
    Submit performs the soft delete; close cancels. The destructive
    button style is reinforced visually inside the modal copy.
    """
    return {
        "type": "modal",
        "callback_id": MODAL_CALLBACK_DELETE_TASK,
        "private_metadata": str(task_id),
        "title": {"type": "plain_text", "text": "Delete task?"},
        "submit": {"type": "plain_text", "text": "Delete"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":warning: This will delete *task #{task_id} — {title}*. "
                        "The task is hidden from the UI immediately; the row "
                        "stays in the audit log so we can trace what happened."
                    ),
                },
            }
        ],
    }


def admin_review_card(
    *,
    task: Any,
    reasoning: str | None = None,
    source_permalink: str | None = None,
) -> list[dict[str, Any]]:
    """CR-03 FR-CR-03-4: admin-only review widget with Confirm/Edit/Reject."""
    owner_label = (
        f"<@{task.owner_user_id}>"
        if task.owner_user_id
        else (task.owner_display_name or "—")
    )
    if (task.extra or {}).get("owner_assumed"):
        owner_label += " _(implicit)_"
    fields = [
        ("Title", task.title or "—"),
        ("Owner", owner_label),
        ("Due", task.due_date.isoformat() if task.due_date else "—"),
        ("Priority", task.priority.value if task.priority else "—"),
    ]
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Task #{task.id} — pending review",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*{label}*\n{value}"}
                for label, value in fields
            ],
        },
    ]
    if reasoning:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"_why detected:_ {reasoning}"}
                ],
            }
        )
    if source_permalink:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"<{source_permalink}|Open source message>",
                    }
                ],
            }
        )
    blocks.append(
        {
            "type": "actions",
            "block_id": f"admin_review_{task.id}",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "action_id": ACTION_ADMIN_CONFIRM_TASK,
                    "text": {"type": "plain_text", "text": "Confirm"},
                    "value": str(task.id),
                },
                {
                    "type": "button",
                    "action_id": ACTION_ADMIN_EDIT_TASK,
                    "text": {"type": "plain_text", "text": "Edit"},
                    "value": str(task.id),
                },
                {
                    "type": "button",
                    "style": "danger",
                    "action_id": ACTION_ADMIN_REJECT_TASK,
                    "text": {"type": "plain_text", "text": "Reject"},
                    "value": str(task.id),
                },
            ],
        }
    )
    return blocks


def admin_review_resolved_message(
    *, task_id: int, action: str, actor: str | None
) -> list[dict[str, Any]]:
    """Replace the admin review card after Confirm/Reject so it's obvious
    no further action is needed."""
    icons = {"confirm": ":white_check_mark:", "reject": ":x:", "edit": ":pencil2:"}
    label = {"confirm": "Confirmed", "reject": "Rejected", "edit": "Edited"}
    actor_s = f" <@{actor}>" if actor else ""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{icons.get(action, ':arrows_counterclockwise:')} "
                    f"Task *#{task_id}* — {label.get(action, action)}{actor_s}."
                ),
            },
        }
    ]


def weekly_plan_blocks(
    *,
    week_start,
    week_end,
    tasks: list[Any],
) -> list[dict[str, Any]]:
    """Sunday-night DM showing each assignee their backlog tasks for the
    upcoming week with Accept / Later buttons per row."""
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Weekly plan {week_start.isoformat()} – {week_end.isoformat()}",
                "emoji": True,
            },
        },
    ]
    if not tasks:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": ":sparkles: No tasks for next week — relax.",
                },
            }
        )
        return blocks
    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "Pick which tasks you'll take. *Accept* → To Do. "
                    "*Later* → stays in Backlog."
                ),
            },
        }
    )
    blocks.append({"type": "divider"})
    for t in tasks:
        meta = f"`{t.status.value}`"
        if t.due_date:
            meta += f" · due {t.due_date.isoformat()}"
        if t.priority:
            meta += f" · priority {t.priority.value}"
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*#{t.id}* {t.title}\n{meta}",
                },
            }
        )
        blocks.append(
            {
                "type": "actions",
                "block_id": f"weekly_plan_{t.id}",
                "elements": [
                    {
                        "type": "button",
                        "style": "primary",
                        "action_id": ACTION_WEEKLY_ACCEPT,
                        "text": {"type": "plain_text", "text": "Accept"},
                        "value": str(t.id),
                    },
                    {
                        "type": "button",
                        "action_id": ACTION_WEEKLY_DEFER,
                        "text": {"type": "plain_text", "text": "Later"},
                        "value": str(t.id),
                    },
                ],
            }
        )
    return blocks


def subscriptions_modal(tasks: list[Any]) -> dict[str, Any]:
    """Modal listing every task the viewer subscribes to with per-row
    Unsubscribe buttons."""
    blocks: list[dict[str, Any]] = []
    if not tasks:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "No active subscriptions.",
                },
            }
        )
    else:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Tracking ({len(tasks)})*",
                },
            }
        )
        blocks.append({"type": "divider"})
        for t in tasks:
            meta = f"`{t.status.value}`"
            if t.due_date:
                meta += f" · due {t.due_date.isoformat()}"
            if t.owner_user_id:
                meta += f" · owner <@{t.owner_user_id}>"
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*#{t.id}* {t.title}\n{meta}",
                    },
                    "accessory": {
                        "type": "button",
                        "style": "danger",
                        "action_id": ACTION_UNSUBSCRIBE_IN_MODAL,
                        "text": {"type": "plain_text", "text": "Unsubscribe"},
                        "value": str(t.id),
                    },
                }
            )

    return {
        "type": "modal",
        "callback_id": MODAL_CALLBACK_SUBSCRIPTIONS,
        "title": {"type": "plain_text", "text": "Subscriptions"},
        "close": {"type": "plain_text", "text": "Done"},
        "blocks": blocks,
    }
