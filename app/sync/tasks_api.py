from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy.orm import Session
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.logging_setup import get_logger
from app.models import GoogleTasksSync, SyncStatus, Task

log = get_logger(__name__)


def _task_body(task: Task) -> dict[str, Any]:
    body: dict[str, Any] = {"title": task.title}
    if task.description:
        body["notes"] = task.description
    if task.due_date:
        # Google Tasks API requires an RFC3339 datetime.
        due_dt = datetime.combine(task.due_date, time(0, 0, tzinfo=timezone.utc))
        body["due"] = due_dt.isoformat()
    if task.status and task.status.value == "done":
        body["status"] = "completed"
    else:
        body["status"] = "needsAction"
    return body


class GoogleTasksSyncService:
    def __init__(
        self,
        *,
        credentials: Credentials,
        tasklist_id: str,
        google_user_id: str | None = None,
    ) -> None:
        self._service = build(
            "tasks", "v1", credentials=credentials, cache_discovery=False
        )
        self._tasklist_id = tasklist_id
        self._google_user_id = google_user_id

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        retry=retry_if_exception_type(HttpError),
    )
    def _insert(self, body: dict[str, Any]) -> dict[str, Any]:
        return (
            self._service.tasks()
            .insert(tasklist=self._tasklist_id, body=body)
            .execute()
        )

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        retry=retry_if_exception_type(HttpError),
    )
    def _patch(self, google_task_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return (
            self._service.tasks()
            .patch(tasklist=self._tasklist_id, task=google_task_id, body=body)
            .execute()
        )

    def sync(self, session: Session, task: Task) -> GoogleTasksSync:
        record = (
            session.query(GoogleTasksSync).filter_by(task_id=task.id).one_or_none()
        )
        if record is None:
            record = GoogleTasksSync(
                task_id=task.id,
                google_user_id=self._google_user_id,
                tasklist_id=self._tasklist_id,
                status=SyncStatus.pending,
            )
            session.add(record)
            session.flush()

        body = _task_body(task)
        record.attempts += 1
        try:
            if record.google_task_id is None:
                created = self._insert(body)
                record.google_task_id = created["id"]
                task.google_tasks_id = created["id"]
            else:
                self._patch(record.google_task_id, body)

            record.status = SyncStatus.success
            record.last_error = None
            record.last_synced_at = datetime.now(timezone.utc)
        except HttpError as e:
            record.status = SyncStatus.failed
            record.last_error = str(e)
            log.warning("google_tasks_sync_failed", task_id=task.id, error=str(e))
            raise
        finally:
            session.flush()
        return record
