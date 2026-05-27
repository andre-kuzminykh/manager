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
        sender=None,
    ) -> None:
        self._settings = settings
        self._sheets_factory = sheets_service_factory
        self._google_tasks_factory = google_tasks_service_factory
        self._sender = sender  # optional: used to post the task card (CR-01)

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
                # FR-CR-05-206 — classify the strategic direction here if it
                # wasn't set at ingest (modal-created Slack tasks skip the
                # FR-CR-05-200 classification). One gpt-4o-mini call per NEW
                # task; create_task_from_draft then carries it into extra.
                self._ensure_direction(session, draft)
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
            self._morph_widget_into_task_card(
                task_id=task_id,
                draft_id=draft_id,
                source_metadata=source_metadata,
            )

        return entity_type, entity_id, summary

    def _ensure_direction(self, session, draft) -> None:
        """Backfill the draft's strategic direction at finalize-time (one
        gpt-4o-mini call) when ingest didn't set it. Best-effort — gated on an
        API key, never blocks task creation."""
        payload = dict(draft.payload or {})
        if (payload.get("direction") or "").strip() or not (payload.get("title") or "").strip():
            return
        if not getattr(self._settings, "openai_api_key", ""):
            return
        try:
            from openai import OpenAI

            from app.intent.llm_backends import OpenAIBackend
            from app.services.task_direction import ensure_direction

            backend = OpenAIBackend(
                client=OpenAI(api_key=self._settings.openai_api_key), model="gpt-4o-mini"
            )
            if ensure_direction(payload, llm_backend=backend, model="gpt-4o-mini"):
                draft.payload = payload
                session.flush()
        except Exception as e:  # noqa: BLE001 — must never block task creation
            log.warning("finalize_direction_classify_failed", error=str(e))

    def _morph_widget_into_task_card(
        self,
        *,
        task_id: int,
        draft_id: int,
        source_metadata: dict[str, Any],
    ) -> None:
        """CR-02+: turn the draft widget into the persistent task card in
        place via chat.update, and DM a copy to the task owner as a log."""
        if self._sender is None:
            return

        from app.models import Task
        from app.slack_bot import blocks as bk

        with session_scope() as session:
            task = session.get(Task, task_id)
            draft = session.get(ActionDraft, draft_id)
            if task is None or draft is None:
                return

            channel_card = bk.task_card(
                task=task, viewer_slack_user_id=task.owner_user_id
            )
            dm_card = bk.task_card(
                task=task, viewer_slack_user_id=task.owner_user_id
            )

            # 1) Materialise the card in the channel. If the draft already
            #    has a widget (Confirm/Edit/Ignore posted earlier) — morph
            #    it in place via chat.update. Otherwise post a fresh
            #    task-card as a thread reply under the source message
            #    (always-create mention flow).
            channel = draft.card_channel or source_metadata.get("conversation_id")
            ts = draft.card_ts
            thread_ts = (
                source_metadata.get("thread_ts")
                or source_metadata.get("message_ts")
            )
            if channel and ts and hasattr(self._sender, "update_message"):
                try:
                    self._sender.update_message(
                        channel=channel,
                        ts=ts,
                        blocks=channel_card,
                        text=f":clipboard: Task #{task_id}: {task.title}",
                    )
                    task.card_channel = channel
                    task.card_ts = ts
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "channel_card_update_failed",
                        task_id=task_id,
                        error=str(e),
                    )
            elif channel:
                try:
                    resp = self._sender.post_message(
                        channel=channel,
                        thread_ts=thread_ts,
                        blocks=channel_card,
                        text=f":clipboard: Task #{task_id}: {task.title}",
                    )
                    if isinstance(resp, dict):
                        task.card_channel = channel
                        task.card_ts = resp.get("ts")
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "channel_card_post_failed",
                        task_id=task_id,
                        error=str(e),
                    )

            # 2) DM a mirror copy to the task owner, so they keep a running
            #    log of tasks assigned to them. The ts is also pinned on
            #    the owner's TaskSubscription row so every subsequent
            #    broadcast about this task lands as a thread reply there.
            from app.models import TaskSubscription

            owner_id = task.owner_user_id or draft.created_by_slack_user_id
            if owner_id:
                try:
                    resp = self._sender.post_message(
                        channel=owner_id,
                        blocks=dm_card,
                        text=f":clipboard: Task #{task_id}: {task.title}",
                    )
                    if isinstance(resp, dict):
                        task.dm_channel = owner_id
                        task.dm_ts = resp.get("ts")
                        owner_sub = (
                            session.query(TaskSubscription)
                            .filter_by(task_id=task.id, slack_user_id=owner_id)
                            .one_or_none()
                        )
                        if owner_sub is not None:
                            owner_sub.dm_ts = resp.get("ts")
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "dm_mirror_failed", task_id=task_id, error=str(e)
                    )
            session.flush()

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
