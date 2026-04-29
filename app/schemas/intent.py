from datetime import date, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class IntentType(str, Enum):
    create_task = "create_task"
    create_meeting = "create_meeting"
    update_task = "update_task"
    update_meeting = "update_meeting"
    no_action = "no_action"


class InvocationType(str, Enum):
    passive = "passive"
    mention = "mention"
    shortcut = "shortcut"


class TaskDraft(BaseModel):
    """Structured extraction of a task from a Slack message."""

    title: str = Field(..., description="Short actionable title in imperative form.")
    description: str | None = Field(None, description="Additional details or context.")
    owner_display_name: str | None = Field(
        None, description="Human-readable owner hint, e.g. '@Ivan' or 'me'."
    )
    owner_user_id: str | None = Field(
        None, description="Resolved Slack user id if already known."
    )
    priority: Literal["low", "medium", "high", "urgent"] = "medium"
    due_date: date | None = Field(None, description="Due date in ISO format YYYY-MM-DD.")
    owner_assumed: bool = Field(
        False,
        description="True when the owner slot is a fallback to the message author rather than an explicit assignment.",
    )


class MeetingDraft(BaseModel):
    title: str
    notes: str | None = None
    participants: list[str] = Field(default_factory=list)
    datetime_at: datetime | None = Field(
        None, description="ISO 8601 datetime with timezone if known."
    )
    timezone: str | None = None


class IntentClassification(BaseModel):
    """Result of running the intent pipeline on a single message.

    FR-CR-05-05 — a single message can carry multiple tasks
    («сделать презу к завтра и отчёт к пятнице»). The canonical
    extraction lives on ``tasks`` (a list); the legacy ``task``
    field is kept for back-compat and mirrors ``tasks[0]`` — every
    existing reader of ``classification.task`` keeps working.
    """

    intent: IntentType
    confidence: float = Field(..., ge=0.0, le=1.0)
    reasoning: str | None = None
    task: TaskDraft | None = None
    tasks: list[TaskDraft] = Field(default_factory=list)
    meeting: MeetingDraft | None = None

    @model_validator(mode="after")
    def _sync_task_and_tasks(self) -> "IntentClassification":
        """Keep ``task`` and ``tasks`` consistent.

        - If only ``task`` is set, mirror it as ``tasks=[task]``.
        - If only ``tasks`` is set, mirror ``tasks[0]`` to ``task``.
        - If both are set, ``tasks`` wins (new code path) and
          ``task`` is rewritten to ``tasks[0]``.
        """
        if self.tasks:
            object.__setattr__(self, "task", self.tasks[0])
        elif self.task is not None:
            object.__setattr__(self, "tasks", [self.task])
        return self

    def draft_payload(self) -> dict | None:
        """Legacy single-task draft payload — used by callers that
        haven't been ported to the multi-task shape yet.
        """
        if self.intent in (IntentType.create_task, IntentType.update_task) and self.task:
            return self.task.model_dump(mode="json")
        if self.intent in (IntentType.create_meeting, IntentType.update_meeting) and self.meeting:
            return self.meeting.model_dump(mode="json")
        return None
