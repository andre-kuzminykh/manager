from datetime import date, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


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


class MeetingDraft(BaseModel):
    title: str
    notes: str | None = None
    participants: list[str] = Field(default_factory=list)
    datetime_at: datetime | None = Field(
        None, description="ISO 8601 datetime with timezone if known."
    )
    timezone: str | None = None


class IntentClassification(BaseModel):
    intent: IntentType
    confidence: float = Field(..., ge=0.0, le=1.0)
    reasoning: str | None = None
    task: TaskDraft | None = None
    meeting: MeetingDraft | None = None

    def draft_payload(self) -> dict | None:
        if self.intent in (IntentType.create_task, IntentType.update_task) and self.task:
            return self.task.model_dump(mode="json")
        if self.intent in (IntentType.create_meeting, IntentType.update_meeting) and self.meeting:
            return self.meeting.model_dump(mode="json")
        return None
