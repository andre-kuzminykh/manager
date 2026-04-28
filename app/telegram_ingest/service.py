"""Process Telegram source messages through the existing intent
pipeline and persist the resulting tasks in our local DB."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.context.retriever import ContextWindow
from app.intent import IntentClassifier
from app.logging_setup import get_logger
from app.models import (
    ActionDraft,
    ActionDraftState,
    ProcessedTelegramMessage,
    Task,
    TaskSourceKind,
)
from app.orchestrator import Orchestrator
from app.persistence import create_task_from_draft
from app.schemas.intent import InvocationType, IntentType
from app.telegram_ingest.reader import TelegramSourceMessage

log = get_logger(__name__)


@dataclass
class IngestReport:
    """Counters returned from a batch run."""

    seen: int = 0
    skipped_already_processed: int = 0
    skipped_empty_text: int = 0
    no_action: int = 0
    tasks_created: int = 0
    errors: int = 0
    error_samples: list[str] = field(default_factory=list)


def _build_window(msg: TelegramSourceMessage) -> ContextWindow:
    """Wrap a TelegramSourceMessage in a Slack-shaped ContextWindow.

    The intent pipeline reads ``conversation_id`` / ``source_ts`` /
    ``source_message`` — we hand it Telegram identifiers in the same
    fields so no pipeline code changes.
    """
    return ContextWindow(
        conversation_id=str(msg.chat_id),
        source_ts=str(msg.message_id),
        thread_ts=str(msg.reply_to) if msg.reply_to else None,
        source_message={
            "ts": str(msg.message_id),
            "thread_ts": str(msg.reply_to) if msg.reply_to else None,
            "user": str(msg.user_id) if msg.user_id else (msg.user_name or "tg_unknown"),
            "text": msg.text,
            "subtype": None,
        },
    )


def _telegram_permalink(msg: TelegramSourceMessage) -> str | None:
    """Best-effort link to the original Telegram message.

    Public chats: ``https://t.me/c/<chat>/<msg>`` (works for super-
    groups / channels; renders as a link card in Slack and Sheets).
    Private chats: returns None — there's no shareable URL.
    """
    if msg.chat_id < 0:
        # Telegram supergroups / channels live at chat ids < 0 with a
        # `-100` prefix on the public id. Strip that prefix per t.me's
        # /c/<id>/<msg> URL scheme.
        public = abs(msg.chat_id)
        if public > 1000000000000:
            public -= 1000000000000
        return f"https://t.me/c/{public}/{msg.message_id}"
    return None


class TelegramIngestService:
    """Pulls a batch of Telegram messages through the intent pipeline
    and writes confirmed tasks into the local DB.

    The service is driven externally — by a one-shot CLI
    (``ops/telegram_ingest.py``) or the history migration script.
    Each message either yields a Task (status = create_task) or is
    recorded as "no_action" so the next ingest pass skips it.
    """

    def __init__(
        self,
        *,
        classifier: IntentClassifier,
        orchestrator: Orchestrator,
    ) -> None:
        self._classifier = classifier
        self._orchestrator = orchestrator

    def process_one(
        self,
        session: Session,
        message: TelegramSourceMessage,
    ) -> Task | None:
        """Process a single Telegram message inside an existing
        transaction. Returns the created Task, or None if the message
        was skipped or yielded no action.

        Idempotent: if the (chat_id, message_id) is already in
        ``processed_telegram_messages``, we do nothing.
        """
        existing = session.get(
            ProcessedTelegramMessage, (message.chat_id, message.message_id)
        )
        if existing is not None:
            return None
        if not message.is_textual:
            session.add(
                ProcessedTelegramMessage(
                    chat_id=message.chat_id,
                    message_id=message.message_id,
                    processed_at=datetime.now(timezone.utc),
                    task_id=None,
                )
            )
            return None

        window = _build_window(message)
        classification = self._classifier.classify(
            context=window,
            invocation_type=InvocationType.passive,
            known_employees=None,
        )

        if classification.intent != IntentType.create_task or classification.task is None:
            session.add(
                ProcessedTelegramMessage(
                    chat_id=message.chat_id,
                    message_id=message.message_id,
                    processed_at=datetime.now(timezone.utc),
                    task_id=None,
                )
            )
            return None

        # FR-CR-04-30: when the LLM didn't extract an owner (the
        # common case for TG ingest where we run with no employees
        # table), fall back to the sender's identity so the card /
        # Sheet show "Andre" instead of the raw user id, AND so the
        # task gets a real `owner_user_id` (without it the card
        # renders only the bystander Subscribe button — `is_owner`
        # never matches a None owner).
        if not classification.task.owner_user_id and message.user_id:
            classification.task.owner_user_id = str(message.user_id)
        if not classification.task.owner_display_name and message.user_name:
            classification.task.owner_display_name = message.user_name

        # Persist context + inference + draft, then immediately
        # finalise into a Task. This mirrors the @mention path: high
        # confidence → create now, ask for follow-up later if fields
        # are missing.
        snapshot = self._orchestrator.persist_context_snapshot(
            session, window.to_snapshot_dict()
        )
        inference = self._orchestrator.persist_inference(
            session,
            context_snapshot=snapshot,
            classification=classification,
            invocation_type=InvocationType.passive,
        )
        author_id = window.source_message["user"]
        draft = self._orchestrator.create_draft(
            session,
            inference=inference,
            classification=classification,
            created_by_slack_user_id=str(author_id) if author_id else None,
            slack_message_ts=str(message.message_id),
        )
        task = create_task_from_draft(
            session,
            draft=draft,
            source={
                "kind": TaskSourceKind.telegram.value,
                "conversation_id": str(message.chat_id),
                "message_ts": str(message.message_id),
                "thread_ts": str(message.reply_to) if message.reply_to else None,
                "permalink": _telegram_permalink(message),
            },
            context_snapshot_id=snapshot.id,
            fallback_author_slack_id=str(author_id) if author_id else None,
        )

        session.add(
            ProcessedTelegramMessage(
                chat_id=message.chat_id,
                message_id=message.message_id,
                processed_at=datetime.now(timezone.utc),
                task_id=task.id,
            )
        )
        return task

    def prepare_draft(
        self,
        session: Session,
        message: TelegramSourceMessage,
    ):
        """Confirm-first variant of `process_one` (FR-CR-04-32).

        Same up-front classification as `process_one`, but stops at the
        ActionDraft and skips Task creation. Used when a task-shaped
        message arrives in a group / supergroup / channel — we DM a
        confirmation widget to the author + admins and only finalise
        into a Task on Accept.

        Returns the persisted ``ActionDraft`` (state = ``proposed``)
        or ``None`` when the message was already processed, has no
        usable text, or didn't classify as a task.

        Idempotent: a second call with the same (chat_id, message_id)
        returns ``None`` (the bookmark in
        ``processed_telegram_messages`` short-circuits us).
        """
        existing = session.get(
            ProcessedTelegramMessage, (message.chat_id, message.message_id)
        )
        if existing is not None:
            return None
        if not message.is_textual:
            session.add(
                ProcessedTelegramMessage(
                    chat_id=message.chat_id,
                    message_id=message.message_id,
                    processed_at=datetime.now(timezone.utc),
                    task_id=None,
                )
            )
            return None

        window = _build_window(message)
        classification = self._classifier.classify(
            context=window,
            invocation_type=InvocationType.passive,
            known_employees=None,
        )

        if classification.intent != IntentType.create_task or classification.task is None:
            session.add(
                ProcessedTelegramMessage(
                    chat_id=message.chat_id,
                    message_id=message.message_id,
                    processed_at=datetime.now(timezone.utc),
                    task_id=None,
                )
            )
            return None

        # Same author/owner fallbacks as the immediate-create path so
        # the confirm widget shows a meaningful preview (Andre vs.
        # raw numeric id).
        if not classification.task.owner_user_id and message.user_id:
            classification.task.owner_user_id = str(message.user_id)
        if not classification.task.owner_display_name and message.user_name:
            classification.task.owner_display_name = message.user_name

        snapshot = self._orchestrator.persist_context_snapshot(
            session, window.to_snapshot_dict()
        )
        inference = self._orchestrator.persist_inference(
            session,
            context_snapshot=snapshot,
            classification=classification,
            invocation_type=InvocationType.passive,
        )
        author_id = window.source_message["user"]
        draft = self._orchestrator.create_draft(
            session,
            inference=inference,
            classification=classification,
            created_by_slack_user_id=str(author_id) if author_id else None,
            slack_message_ts=str(message.message_id),
        )

        # Stash the bits `create_task_from_draft` will need on Accept.
        # Stored under a single underscore-prefixed key so they're
        # easy to pop before persisting the Task.
        payload = dict(draft.payload or {})
        payload["_pending"] = {
            "source_kind": "telegram",
            "conversation_id": str(message.chat_id),
            "message_ts": str(message.message_id),
            "thread_ts": str(message.reply_to) if message.reply_to else None,
            "permalink": _telegram_permalink(message),
            "fallback_author": str(author_id) if author_id else None,
            "context_snapshot_id": snapshot.id,
            "source_chat_id": message.chat_id,
            "source_message_id": message.message_id,
        }
        draft.payload = payload
        session.flush()

        # Bookmark the source message so it doesn't get re-classified
        # on the next ingest pass. We attach `task_id=None` for now —
        # if the user Accepts, the confirm handler updates this row
        # to point at the new Task.
        session.add(
            ProcessedTelegramMessage(
                chat_id=message.chat_id,
                message_id=message.message_id,
                processed_at=datetime.now(timezone.utc),
                task_id=None,
            )
        )
        return draft

    def process_batch(
        self,
        session: Session,
        messages: list[TelegramSourceMessage],
    ) -> IngestReport:
        """Process a slice of messages, returning a counters report.

        Errors per-message are caught so one bad row doesn't abort
        the whole batch — we record the chat/msg id in the report
        and move on. The caller decides whether to commit or roll
        back the transaction.
        """
        report = IngestReport()
        for m in messages:
            report.seen += 1
            try:
                if not m.is_textual:
                    report.skipped_empty_text += 1
                    session.add(
                        ProcessedTelegramMessage(
                            chat_id=m.chat_id,
                            message_id=m.message_id,
                            processed_at=datetime.now(timezone.utc),
                            task_id=None,
                        )
                    )
                    continue
                existing = session.get(
                    ProcessedTelegramMessage, (m.chat_id, m.message_id)
                )
                if existing is not None:
                    report.skipped_already_processed += 1
                    continue
                task = self.process_one(session, m)
                if task is None:
                    report.no_action += 1
                else:
                    report.tasks_created += 1
            except Exception as e:  # noqa: BLE001
                report.errors += 1
                if len(report.error_samples) < 5:
                    report.error_samples.append(
                        f"chat={m.chat_id} msg={m.message_id}: {e!s}"
                    )
                log.warning(
                    "telegram_ingest_message_failed",
                    chat_id=m.chat_id,
                    message_id=m.message_id,
                    error=str(e),
                )
                # Try to record this as processed so we don't loop on it forever.
                # If even this fails (e.g. transaction is broken), the
                # surrounding session_scope rollback will still leave the
                # row unprocessed — that's acceptable for an MVP.
                try:
                    session.add(
                        ProcessedTelegramMessage(
                            chat_id=m.chat_id,
                            message_id=m.message_id,
                            processed_at=datetime.now(timezone.utc),
                            task_id=None,
                        )
                    )
                    session.flush()
                except Exception:
                    session.rollback()
        return report
