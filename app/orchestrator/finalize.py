"""FinalizeService: turns a confirmed ActionDraft into a persisted entity and
best-effort syncs to Google Sheets / Google Tasks.

Sync errors never abort persistence — the DB is the source of truth. Failures
are recorded on the *Sync records so they can be retried.
"""
from __future__ import annotations

from typing import Any

from app.config import Settings
from app.db import session_scope
from app.logging_setup import get_logger
from app.models import ActionDraft, ActionDraftState, AuditLog
from app.models.intent import IntentType as IntentTypeEnum
from app.persistence import (
    create_meeting_from_draft,
    create_task_from_draft,
    summarize_task,
)
from app.sync.sheets import SheetsSyncService
from app.sync.tasks_api import GoogleTasksSyncService

log = get_logger(__name__)


class FinalizeService:
    def __init__(
        self,
        *,
        settings: Settings,
        sheets_service_factory=None,
        google_tasks_service_factory=None,
    ) -> None:
        self._settings = settings
        self._sheets_factory = sheets_service_factory
        self._google_tasks_factory = google_tasks_service_factory

    def finalize_draft(
        self,
        *,
        draft_id: int,
        source_metadata: dict[str, Any],
    ) -> tuple[str, int, str]:
        """Persist entity + sync. Returns (entity_type, entity_id, summary)."""
        with session_scope() as session:
            draft = session.get(ActionDraft, draft_id)
            if draft is None:
                raise ValueError(f"Draft {draft_id} not found")
            if draft.state == ActionDraftState.confirmed:
                # Idempotency: if already confirmed, short-circuit.
                raise ValueError(f"Draft {draft_id} already confirmed")

            source = {
                "conversation_id": source_metadata.get("conversation_id"),
                "message_ts": source_metadata.get("message_ts"),
                "thread_ts": source_metadata.get("thread_ts"),
                "permalink": source_metadata.get("permalink"),
            }
            context_snapshot_id = source_metadata.get("context_snapshot_id")
            fallback_author = source_metadata.get("source_user_id")

            if draft.intent in (IntentTypeEnum.create_task, IntentTypeEnum.update_task):
                task = create_task_from_draft(
                    session,
                    draft=draft,
                    source=source,
                    context_snapshot_id=context_snapshot_id,
                    fallback_author_slack_id=fallback_author,
                )
                entity_type = "task"
                entity_id = task.id
                summary = summarize_task(task)

                session.add(
                    AuditLog(
                        category="entity",
                        action="task_created",
                        entity_type="task",
                        entity_id=str(task.id),
                        actor=draft.created_by_slack_user_id,
                        payload={"draft_id": draft.id, "source": source},
                    )
                )
                # Flush so we can read the task outside this transaction.
                session.flush()
                task_id = task.id
            else:
                meeting = create_meeting_from_draft(
                    session,
                    draft=draft,
                    source=source,
                    context_snapshot_id=context_snapshot_id,
                    fallback_author_slack_id=fallback_author,
                )
                entity_type = "meeting"
                entity_id = meeting.id
                summary = meeting.title
                session.add(
                    AuditLog(
                        category="entity",
                        action="meeting_created",
                        entity_type="meeting",
                        entity_id=str(meeting.id),
                        actor=draft.created_by_slack_user_id,
                        payload={"draft_id": draft.id, "source": source},
                    )
                )
                task_id = None

        # Sync tasks to Google surfaces outside the main transaction.
        if task_id is not None:
            self._sync_task(task_id)

        return entity_type, entity_id, summary

    def _sync_task(self, task_id: int) -> None:
        sheets_service = self._sheets_factory() if self._sheets_factory else None
        gtasks_service = (
            self._google_tasks_factory() if self._google_tasks_factory else None
        )

        if sheets_service is None and gtasks_service is None:
            log.info("sync_skipped_no_google_credentials", task_id=task_id)
            return

        from app.models import Task  # local import to avoid any circularity

        with session_scope() as session:
            task = session.get(Task, task_id)
            if task is None:
                return

            if sheets_service is not None:
                try:
                    sheets_service.sync(session, task)
                except Exception as e:  # noqa: BLE001
                    log.warning("sheets_sync_failed", task_id=task.id, error=str(e))

            if gtasks_service is not None:
                try:
                    gtasks_service.sync(session, task)
                except Exception as e:  # noqa: BLE001
                    log.warning("google_tasks_sync_failed", task_id=task.id, error=str(e))
